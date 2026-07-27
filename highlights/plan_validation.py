"""Strict geometry-plan validation for the SolidWorks design pipeline (stage 1 and 2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from intentcad.core.schemas.edit_plan import (
    AddFeatureOp,
    DeleteFeatureOp,
    EditPlan,
    FeatureTreeState,
    ModifyDimensionOp,
    SuppressFeatureOp,
)
from intentcad.core.schemas.geometry_contract import (
    EXTRUDE_BOSS_MODE_ALIASES,
    EXTRUDE_BOSS_MODES,
    REJECTED_EXTRUDE_BOSS_MODES,
    UNSUPPORTED_GEOMETRY_PLAN_OPERATIONS,
    resolve_sw_tool_name,
)
from intentcad.core.schemas.geometry_plan import GeometryPlan, GeometryPlanStep

PlanValidationCode = Literal[
    "empty_plan",
    "unsupported_operation",
    "unknown_operation",
    "no_tool_mapping",
    "incomplete_step",
    "invalid_extrude_operation",
    "plan_incomplete",
]


class PlanValidationIssue(BaseModel):
    """One structured validation failure."""

    model_config = ConfigDict(extra="forbid")

    code: PlanValidationCode
    message: str
    step: int | None = None
    operation: str | None = None


class PlanValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    errors: list[PlanValidationIssue] = []


class PlanValidationError(Exception):
    """Raised when a geometry plan fails strict validation before staging or execution."""

    def __init__(self, errors: list[PlanValidationIssue]) -> None:
        self.errors = errors
        super().__init__(_format_errors(errors))


def _format_errors(errors: list[PlanValidationIssue]) -> str:
    return "; ".join(e.message for e in errors)


def _canonical_extrude_boss_mode(params: dict[str, Any]) -> str:
    raw = params.get("operation", params.get("op", "new_body"))
    key = str(raw).lower().strip()
    if key in REJECTED_EXTRUDE_BOSS_MODES:
        return key
    return EXTRUDE_BOSS_MODE_ALIASES.get(key, key)


def _check_step_parameters(
    step: GeometryPlanStep,
    *,
    body_ids: set[str],
) -> PlanValidationIssue | None:
    """Reject incomplete steps using the same rules as normalize_step_arguments."""
    from intentcad.agents.geometry_executor import normalize_step_arguments

    op = step.operation.lower().strip()
    try:
        normalize_step_arguments(
            op, dict(step.parameters), plan_state={"body_ids": body_ids}
        )
    except ValueError as exc:
        return PlanValidationIssue(
            code="incomplete_step",
            message=str(exc),
            step=step.step,
            operation=step.operation,
        )
    return None


def validate_geometry_plan(plan: GeometryPlan) -> PlanValidationResult:
    """Return structured validation result; does not mutate the plan."""
    from intentcad.agents.geometry_executor import validate_plan_completeness

    issues: list[PlanValidationIssue] = []

    if not plan.steps:
        issues.append(
            PlanValidationIssue(
                code="empty_plan",
                message="Geometry plan has no steps.",
                step=None,
                operation=None,
            )
        )
        return PlanValidationResult(valid=False, errors=issues)

    from intentcad.agents.geometry_executor import _register_created_body

    created_bodies: set[str] = set()
    for step in sorted(plan.steps, key=lambda s: s.step):
        op = step.operation.lower().strip()

        if op in UNSUPPORTED_GEOMETRY_PLAN_OPERATIONS:
            issues.append(
                PlanValidationIssue(
                    code="unsupported_operation",
                    message=(
                        f"Operation {step.operation!r} is not in the SolidWorks design "
                        f"pipeline contract. Supported steps are listed in "
                        f"geometry_contract.GEOMETRY_PLAN_OPERATIONS; use "
                        f"pocket with add_circle for through-holes."
                    ),
                    step=step.step,
                    operation=step.operation,
                )
            )
            continue

        if resolve_sw_tool_name(op) is None:
            issues.append(
                PlanValidationIssue(
                    code="no_tool_mapping",
                    message=f"No SolidWorks tool mapping for operation {step.operation!r}.",
                    step=step.step,
                    operation=step.operation,
                )
            )
            continue

        if op in ("extrude", "pocket"):
            boss_op = _canonical_extrude_boss_mode(step.parameters)
            if boss_op in REJECTED_EXTRUDE_BOSS_MODES or boss_op not in EXTRUDE_BOSS_MODES:
                issues.append(
                    PlanValidationIssue(
                        code="invalid_extrude_operation",
                        message=(
                            f"Step {step.step} ({op}): boss operation {boss_op!r} is not "
                            f"supported. Use one of: {sorted(EXTRUDE_BOSS_MODES)!r}."
                        ),
                        step=step.step,
                        operation=step.operation,
                    )
                )
                continue

        param_issue = _check_step_parameters(step, body_ids=created_bodies)
        if param_issue is not None:
            issues.append(param_issue)

        _register_created_body(step, created_bodies)

    for message in validate_plan_completeness(plan):
        issues.append(
            PlanValidationIssue(
                code="plan_incomplete",
                message=message,
                step=None,
                operation=None,
            )
        )

    return PlanValidationResult(valid=len(issues) == 0, errors=issues)


class EditPlanValidationError(Exception):
    def __init__(self, errors: list[PlanValidationIssue]) -> None:
        self.errors = errors
        super().__init__(_format_errors(errors))


def validate_edit_plan(
    plan: EditPlan, tree: FeatureTreeState
) -> PlanValidationResult:
    issues: list[PlanValidationIssue] = []
    if not plan.operations:
        issues.append(
            PlanValidationIssue(
                code="empty_plan",
                message="Edit plan has no operations.",
                step=None,
                operation=None,
            )
        )
        return PlanValidationResult(valid=False, errors=issues)

    feature_ids = {f.feature_id for f in tree.features}
    dim_names: set[str] = set()
    for feat in tree.features:
        dim_names.update(feat.dimensions.keys())

    for idx, op in enumerate(plan.operations, start=1):
        if isinstance(op, ModifyDimensionOp):
            if op.feature_id not in feature_ids:
                issues.append(
                    PlanValidationIssue(
                        code="plan_incomplete",
                        message=f"Unknown feature_id {op.feature_id!r}.",
                        step=idx,
                        operation=op.op,
                    )
                )
            if op.dimension_name not in dim_names:
                issues.append(
                    PlanValidationIssue(
                        code="plan_incomplete",
                        message=f"Unknown dimension_name {op.dimension_name!r}.",
                        step=idx,
                        operation=op.op,
                    )
                )
            if op.new_value_mm <= 0.0:
                issues.append(
                    PlanValidationIssue(
                        code="incomplete_step",
                        message="new_value_mm must be positive.",
                        step=idx,
                        operation=op.op,
                    )
                )
        elif isinstance(op, (SuppressFeatureOp, DeleteFeatureOp)):
            if op.feature_id not in feature_ids:
                issues.append(
                    PlanValidationIssue(
                        code="plan_incomplete",
                        message=f"Unknown feature_id {op.feature_id!r}.",
                        step=idx,
                        operation=op.op,
                    )
                )
        elif isinstance(op, AddFeatureOp):
            if not op.plan_steps:
                issues.append(
                    PlanValidationIssue(
                        code="incomplete_step",
                        message="add_feature requires plan_steps.",
                        step=idx,
                        operation=op.op,
                    )
                )
            mini = GeometryPlan(summary="edit", steps=op.plan_steps)
            nested = validate_geometry_plan(mini)
            issues.extend(nested.errors)

    return PlanValidationResult(valid=len(issues) == 0, errors=issues)


__all__ = [
    "EditPlanValidationError",
    "PlanValidationCode",
    "PlanValidationError",
    "PlanValidationIssue",
    "PlanValidationResult",
    "validate_edit_plan",
    "validate_geometry_plan",
]
