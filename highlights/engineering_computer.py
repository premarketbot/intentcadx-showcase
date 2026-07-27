"""Deterministic engineering report builder — no LLM calls."""

from __future__ import annotations

from intentcad.agents.proposal import DesignProposal, LoadCaseSpec
from intentcad.core.knowledge.citations import Citation
from intentcad.core.knowledge.kb import KnowledgeBase
from intentcad.core.knowledge.materials import MaterialProperties
from intentcad.core.knowledge.processes import ProcessProperties
from intentcad.core.knowledge.structural import StructuralCalculator
from intentcad.core.schemas.design_spec import DesignSpec, FeatureRequest
from intentcad.core.schemas.engineering_report import (
    CalculationResult,
    EngineeringReport,
    EngineeringVerdict,
    Issue,
    IssueSeverity,
    MaterialChoice,
    ProcessChoice,
    StandardCheck,
)

_SHIGLEY_BENDING = Citation(
    source="Shigley's Mechanical Engineering Design, 10th ed.",
    section="Sec 3-3 cantilever bending (M = F*L, stress = Mc/I)",
    license="fair_use_summary",
)
_SHIGLEY_SF = Citation(
    source="Shigley's Mechanical Engineering Design, 10th ed.",
    section="Sec 5-1 safety factor (yield / stress)",
    license="fair_use_summary",
)
_SHIGLEY_FATIGUE = Citation(
    source="Shigley's Mechanical Engineering Design, 10th ed.",
    section="§6-7, Eqs. 6-8 and 6-18 (modified Marin endurance limit)",
    license="fair_use_summary",
)
_SHIGLEY_PRELOAD = Citation(
    source="Shigley's Mechanical Engineering Design, 10th ed.",
    section="Sec 8-7, Eq. 8-27 (torque-preload)",
    license="fair_use_summary",
)
_ISO_BEARING = Citation(
    source="ISO 281:2007 Rolling bearings — Dynamic load ratings and rating life",
    section="§5.1 basic rating life L10",
    license="iso_standard_summary",
)


class EngineeringComputer:
    """Build a cited :class:`EngineeringReport` from a :class:`DesignProposal`."""

    def __init__(
        self,
        kb: KnowledgeBase | None = None,
        calculator: type[StructuralCalculator] = StructuralCalculator,
    ) -> None:
        self._kb = kb or KnowledgeBase()
        self._calc = calculator

    def compute(self, proposal: DesignProposal) -> EngineeringReport:
        spec = self._finalize_design_spec(proposal)
        calculations: list[CalculationResult] = []
        issues: list[Issue] = []
        standards: list[StandardCheck] = []

        material_rec, mat_issues, mat_props = self._resolve_material(
            proposal.proposed_material_name
        )
        issues.extend(mat_issues)

        process_rec, proc_issues, proc_props = self._resolve_process(
            proposal.proposed_process_name
        )
        issues.extend(proc_issues)

        if mat_props and proc_props:
            issues.extend(self._dfm_checks(spec, mat_props, proc_props))

        standards.extend(self._fastener_checks(spec))

        if mat_props:
            calculations.extend(
                self._run_load_cases(
                    proposal.structural_requirements.load_cases,
                    mat_props,
                    proposal.structural_requirements.safety_factor_target,
                    proposal.structural_requirements.fatigue_cycles_required,
                )
            )

        verdict = self._verdict(issues)
        summary = self._summary(verdict, material_rec, process_rec, len(calculations))

        return EngineeringReport(
            spec_reference=spec,
            material_recommendation=material_rec,
            process_recommendation=process_rec,
            calculations=calculations,
            standards_compliance=standards,
            flagged_issues=issues,
            overall_verdict=verdict,
            summary=summary,
        )

    @staticmethod
    def _finalize_design_spec(proposal: DesignProposal) -> DesignSpec:
        spec = proposal.design_spec
        merged_oq = list(spec.open_questions)
        for q in proposal.open_questions:
            if q not in merged_oq:
                merged_oq.append(q)
        is_complete = len(merged_oq) == 0 and spec.is_complete
        return spec.model_copy(update={"open_questions": merged_oq, "is_complete": is_complete})

    def _resolve_material(
        self,
        name: str,
    ) -> tuple[MaterialChoice | None, list[Issue], MaterialProperties | None]:
        query = self._kb.material(name)
        if query is None:
            return (
                None,
                [
                    Issue(
                        severity=IssueSeverity.BLOCKER,
                        category="material",
                        message=f"Unrecognized material name: {name!r}",
                        suggestion="Use a KB material name or alias (e.g. 6061-T6, aluminum 6061).",
                    )
                ],
                None,
            )
        mat: MaterialProperties = query.data
        if not query.is_cited:
            return (
                None,
                [
                    Issue(
                        severity=IssueSeverity.WARNING,
                        category="citation",
                        message=f"Material {mat.name} lacks a verifiable citation in the KB.",
                    )
                ],
                mat,
            )
        return (
            MaterialChoice(
                name=mat.name,
                rationale=(
                    f"Yield {mat.yield_strength_mpa} MPa, min wall {mat.min_wall_thickness_mm} mm "
                    f"(cost index {mat.cost_index})."
                ),
                citation=query.citation,
                alternatives=[],
            ),
            [],
            mat,
        )

    def _resolve_process(
        self,
        name: str,
    ) -> tuple[ProcessChoice | None, list[Issue], ProcessProperties | None]:
        query = self._kb.process(name)
        if query is None:
            return (
                None,
                [
                    Issue(
                        severity=IssueSeverity.BLOCKER,
                        category="process",
                        message=f"Unrecognized process name: {name!r}",
                        suggestion="Use a KB process name (e.g. CNC Mill 3-axis, FDM 3D Printing).",
                    )
                ],
                None,
            )
        proc: ProcessProperties = query.data
        return (
            ProcessChoice(
                name=proc.name,
                rationale=(
                    f"Min wall {proc.min_wall_mm} mm, "
                    f"tolerance +/-{proc.tolerance_capability_mm} mm, "
                    f"lead time {proc.lead_time_typical_days} days."
                ),
                citation=query.citation,
                alternatives=[],
            ),
            [],
            proc,
        )

    def _dfm_checks(
        self,
        spec: DesignSpec,
        mat: MaterialProperties,
        proc: ProcessProperties,
    ) -> list[Issue]:
        issues: list[Issue] = []
        min_wall = max(mat.min_wall_thickness_mm, proc.min_wall_mm)
        for feat in spec.features:
            t = _feature_thickness_mm(feat)
            if t is not None and t < min_wall:
                issues.append(
                    Issue(
                        severity=IssueSeverity.WARNING,
                        category="dfm",
                        message=(
                            f"Feature {feat.name!r} thickness {t} mm is below "
                            f"recommended {min_wall} mm for {proc.name} + {mat.name}."
                        ),
                        suggestion=f"Increase wall to at least {min_wall} mm.",
                        citation=proc.citation,
                    )
                )
        return issues

    def _fastener_checks(self, spec: DesignSpec) -> list[StandardCheck]:
        checks: list[StandardCheck] = []
        for feat in spec.features:
            if "hole" not in feat.kind.lower() and "hole" not in feat.name.lower():
                continue
            dia = feat.parameters.get("diameter_mm", feat.parameters.get("diameter"))
            if dia is None:
                continue
            try:
                dia_f = float(dia)
            except (TypeError, ValueError):
                continue
            query = self._kb.fastener_for_diameter(dia_f)
            if query is None:
                continue
            fastener = query.data
            checks.append(
                StandardCheck(
                    standard="ISO metric fasteners",
                    item=f"{fastener.designation} clearance @ {dia_f} mm hole",
                    status="pass",
                    detail=(
                        f"Hole {dia_f} mm matches {fastener.designation} clearance "
                        f"{fastener.clearance_mm} mm."
                    ),
                    citation=query.citation,
                )
            )
        return checks

    def _run_load_cases(
        self,
        load_cases: list[LoadCaseSpec],
        mat: MaterialProperties,
        sf_target: float,
        fatigue_cycles: int | None,
    ) -> list[CalculationResult]:
        results: list[CalculationResult] = []
        max_stress = 0.0
        for lc in load_cases:
            results.extend(self._eval_load_case(lc, mat, sf_target))
            for c in results:
                if c.name.endswith("bending_stress") and c.output_unit == "MPa":
                    max_stress = max(max_stress, c.output_value)

        if fatigue_cycles is not None and max_stress > 0:
            s_e = self._calc.fatigue_endurance_limit_mpa(
                mat.ultimate_strength_mpa,
                reliability_factor=0.897,
            )
            results.append(
                CalculationResult(
                    name="fatigue_endurance_limit",
                    inputs={
                        "ultimate_strength_mpa": mat.ultimate_strength_mpa,
                        "reliability_factor": 0.897,
                    },
                    output_value=round(s_e, 2),
                    output_unit="MPa",
                    interpretation="Modified Marin endurance limit (90% reliability).",
                    citation=_SHIGLEY_FATIGUE,
                )
            )
            sf_f = self._calc.safety_factor(s_e, max_stress) if max_stress > 0 else float("inf")
            results.append(
                CalculationResult(
                    name="fatigue_safety_factor",
                    inputs={
                        "endurance_limit_mpa": s_e,
                        "applied_stress_mpa": max_stress,
                    },
                    output_value=round(sf_f, 2),
                    output_unit="dimensionless",
                    interpretation=(
                        f"Endurance vs peak bending stress over {fatigue_cycles} cycles."
                    ),
                    citation=_SHIGLEY_FATIGUE,
                )
            )
        return results

    def _eval_load_case(
        self,
        lc: LoadCaseSpec,
        mat: MaterialProperties,
        sf_target: float,
    ) -> list[CalculationResult]:
        p = lc.parameters
        if lc.type == "cantilever_bending":
            return self._cantilever_bending(lc.description, p, mat, sf_target)
        if lc.type == "fastener_preload":
            return self._fastener_preload(lc.description, p)
        if lc.type == "bearing_load":
            return self._bearing_l10(lc.description, p)
        return []

    def _cantilever_bending(
        self,
        label: str,
        p: dict[str, float],
        mat: MaterialProperties,
        sf_target: float,
    ) -> list[CalculationResult]:
        if "force_n" not in p or "length_mm" not in p:
            return []
        force_n = p["force_n"]
        length_mm = p["length_mm"]
        section_estimated = "width_mm" not in p or "height_mm" not in p
        width_mm = p.get("width_mm", 10.0)
        height_mm = p.get("height_mm", 10.0)
        stress = self._calc.bending_stress_mpa(force_n, length_mm, width_mm, height_mm)
        sf = self._calc.safety_factor(stress, mat.yield_strength_mpa)
        stress_interp = "Cantilever worst-case bending stress."
        if section_estimated:
            stress_interp += (
                " Section 10x10 mm estimated (provide width_mm and height_mm for accuracy)."
            )
        return [
            CalculationResult(
                name=f"{label}_bending_stress",
                inputs={
                    "force_n": force_n,
                    "length_mm": length_mm,
                    "width_mm": width_mm,
                    "height_mm": height_mm,
                },
                output_value=round(stress, 3),
                output_unit="MPa",
                interpretation=stress_interp,
                citation=_SHIGLEY_BENDING,
            ),
            CalculationResult(
                name=f"{label}_safety_factor",
                inputs={
                    "applied_stress_mpa": stress,
                    "yield_strength_mpa": mat.yield_strength_mpa,
                },
                output_value=round(sf, 3),
                output_unit="dimensionless",
                interpretation=(
                    f"Static SF={sf:.2f} vs target {sf_target:.2f}."
                    if sf >= sf_target
                    else f"Below target SF {sf_target:.2f}."
                ),
                citation=_SHIGLEY_SF,
            ),
        ]

    def _fastener_preload(self, label: str, p: dict[str, float]) -> list[CalculationResult]:
        params = dict(p)
        if "nominal_diameter_mm" not in params and "bolt_diameter_mm" in params:
            params["nominal_diameter_mm"] = params["bolt_diameter_mm"]
        if "torque_nm" not in params and "preload_force_n" in params:
            dia = params.get("nominal_diameter_mm", 6.0)
            params["torque_nm"] = params["preload_force_n"] * dia * 1e-3
        if "torque_nm" not in params or "nominal_diameter_mm" not in params:
            return []
        preload = self._calc.fastener_preload_n(
            params["torque_nm"],
            params["nominal_diameter_mm"],
            nut_factor=params.get("nut_factor", 0.2),
        )
        return [
            CalculationResult(
                name=f"{label}_preload",
                inputs={
                    "torque_nm": params["torque_nm"],
                    "nominal_diameter_mm": params["nominal_diameter_mm"],
                },
                output_value=round(preload, 1),
                output_unit="N",
                interpretation="Estimated bolt preload from applied torque.",
                citation=_SHIGLEY_PRELOAD,
            )
        ]

    def _bearing_l10(self, label: str, p: dict[str, float]) -> list[CalculationResult]:
        if "basic_dynamic_load_rating_n" not in p or "equivalent_load_n" not in p:
            return []
        l10 = self._calc.bearing_l10_life_million_revolutions(
            p["basic_dynamic_load_rating_n"],
            p["equivalent_load_n"],
        )
        return [
            CalculationResult(
                name=f"{label}_L10",
                inputs={
                    "basic_dynamic_load_rating_n": p["basic_dynamic_load_rating_n"],
                    "equivalent_load_n": p["equivalent_load_n"],
                },
                output_value=round(l10, 3),
                output_unit="million rev",
                interpretation="Basic rating life L10 (90% reliability).",
                citation=_ISO_BEARING,
            )
        ]

    @staticmethod
    def _verdict(issues: list[Issue]) -> EngineeringVerdict:
        if any(i.severity == IssueSeverity.BLOCKER for i in issues):
            return "blocked"
        if any(i.severity == IssueSeverity.WARNING for i in issues):
            return "concerns"
        return "proceed"

    @staticmethod
    def _summary(
        verdict: EngineeringVerdict,
        material: MaterialChoice | None,
        process: ProcessChoice | None,
        n_calcs: int,
    ) -> str:
        parts = [f"Verdict: {verdict}."]
        if material:
            parts.append(f"Material: {material.name}.")
        if process:
            parts.append(f"Process: {process.name}.")
        parts.append(f"{n_calcs} calculation(s) from KB-backed formulas.")
        return " ".join(parts)


def _feature_thickness_mm(feat: FeatureRequest) -> float | None:
    for key in ("thickness_mm", "thickness", "wall_mm", "height_mm"):
        val = feat.parameters.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


__all__ = ["EngineeringComputer"]
