"""IR semantic face and edge references → SolidWorks COM resolution.

Single source of truth for ``body_<id>.face.<direction>`` and
``body_<id>.edge.<dir_a>_<dir_b>`` strings used across sketch and feature
builders.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import Any, Final

from intentcad.adapters.solidworks.com_marshalling import null_dispatch
from intentcad.adapters.solidworks.errors import (
    SolidWorksError,
    SolidWorksFeatureCreationError,
)

logger = logging.getLogger(__name__)

_COM_READ_ERRORS: tuple[type[BaseException], ...] = (AttributeError,)
try:
    from pywintypes import com_error  # type: ignore[import-untyped]

    _COM_READ_ERRORS = (AttributeError, com_error)
except ImportError:  # pragma: no cover - no pywin32 in some CI slices
    pass

# Pattern: "<ir_body_id>.face.<direction>" — body_main, flange_body, body_1, etc.
FACE_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9_]*)\.face\.(top|bottom|front|back|left|right)$"
)

# Edge: <ir_body_id>.edge.top_front — intersection of two named face semantics.
EDGE_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9_]*)\.edge\.(top|bottom|front|back|left|right)_"
    r"(top|bottom|front|back|left|right)$"
)

# Maps semantic face direction to (normal_axis_index, sign).
# IFace2.Normal returns an outward unit normal (nx, ny, nz).
_FACE_DIRECTION_NORMAL: Final[dict[str, tuple[int, float]]] = {
    "top": (2, +1.0),
    "bottom": (2, -1.0),
    "front": (1, +1.0),
    "back": (1, -1.0),
    "right": (0, +1.0),
    "left": (0, -1.0),
}


def is_semantic_face_ref(ref: str) -> bool:
    """True iff ``ref`` matches ``<ir_body_id>.face.<direction>``."""
    return bool(FACE_REF_RE.match(ref))


def parse_face_ref(face_ref: str) -> tuple[str, str]:
    """Parse ``body_main.face.top`` into ``(body_ir_id, direction)``."""
    match = FACE_REF_RE.match(face_ref)
    if match is None:
        raise SolidWorksError(
            f"Invalid face reference format: {face_ref!r}. "
            "Expected: '<body_id>.face.<direction>' where direction is one of "
            "top/bottom/front/back/left/right. "
            "Example: 'body_main.face.top'. "
            "Casual identifiers like 'TOP_FACE' are not accepted."
        )
    return match.group(1), match.group(2)


def is_semantic_edge_ref(ref: str) -> bool:
    """True iff ``ref`` matches the ``body_X.edge.<a>_<b>`` pattern."""
    return bool(EDGE_REF_RE.match(ref))


def _ir_body_map(builder_ctx: Any | None) -> dict[str, str] | None:
    if builder_ctx is None:
        return None
    raw = getattr(builder_ctx, "ir_body_to_tree", None)
    if raw is None:
        return None
    return dict(raw)


def _coerce_edges(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [e for e in raw if e is not None]
    with suppress(TypeError, ValueError):
        return [e for e in list(raw) if e is not None]
    return [raw]


def _call_or_value(obj: Any, attr_name: str) -> Any:
    """Read a COM attribute exposed as either a callable or a materialized value.

    pywin32 with SolidWorks 2026 sometimes pre-materializes properties such as
    ``IFace2.GetEdges`` and ``IEdge.GetTwoAdjacentFaces2`` into tuples at
    attribute access time. Calling such a value raises ``TypeError``.

    Verified via ``scripts/diagnose_fillet.py`` (Sprint 4 Cluster 2).
    """
    raw = getattr(obj, attr_name, None)
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return raw
    if callable(raw):
        try:
            return raw()
        except TypeError:
            return raw
        except _COM_READ_ERRORS:
            return None
    return raw


def _same_dispatch(a: Any, b: Any) -> bool:
    if a is b:
        return True
    oa, ob = getattr(a, "_oleobj_", None), getattr(b, "_oleobj_", None)
    return oa is not None and ob is not None and oa is ob


def _face_normal_triple(face: Any) -> tuple[float, float, float] | None:
    try:
        n = face.Normal
        if n is None or len(n) < 3:
            return None
        return float(n[0]), float(n[1]), float(n[2])
    except Exception:
        return None


def _normals_equal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    *,
    tol: float = 1e-5,
) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol and abs(a[2] - b[2]) < tol


def _adjacent_faces_of_edge(edge: Any) -> tuple[Any, Any] | None:
    """Return the two ``IFace2`` faces bordering ``edge`` if the API exposes them."""
    try:
        raw = _call_or_value(edge, "GetTwoAdjacentFaces2")
    except _COM_READ_ERRORS:
        return None
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        fs = [x for x in raw if x is not None]
        if len(fs) >= 2:
            return fs[0], fs[1]
    with suppress(TypeError, ValueError):
        fs = [x for x in list(raw) if x is not None]
        if len(fs) >= 2:
            return fs[0], fs[1]
    return None


def _edge_borders_resolved_faces(edge: Any, f1: Any, f2: Any) -> bool:
    """True if ``edge`` lies between the same two sheet bodies as ``f1`` and ``f2``."""
    pr = _adjacent_faces_of_edge(edge)
    if pr is None:
        return False
    a, b = pr
    if (_same_dispatch(a, f1) and _same_dispatch(b, f2)) or (
        _same_dispatch(a, f2) and _same_dispatch(b, f1)
    ):
        return True
    n1 = _face_normal_triple(f1)
    n2 = _face_normal_triple(f2)
    na = _face_normal_triple(a)
    nb = _face_normal_triple(b)
    if n1 is None or n2 is None or na is None or nb is None:
        return False
    return (_normals_equal(na, n1) and _normals_equal(nb, n2)) or (
        _normals_equal(na, n2) and _normals_equal(nb, n1)
    )


def _edge_identity_token(edge: Any) -> Any:
    """Stable token for edge identity across pywin32 proxy wrappers.

    Prefer the underlying COM ``_oleobj_`` pointer when present so two Python
    wrappers for the same ``IEdge`` collapse to one token. Fall back to
    ``id(edge)`` for pure mocks without ``_oleobj_``.
    """
    ole = getattr(edge, "_oleobj_", None)
    if ole is not None:
        return ole
    return id(edge)


def _edge_token_key(edge: Any) -> int:
    """Hashable key for token sets (``_oleobj_`` may be unhashable on live COM)."""
    tok = _edge_identity_token(edge)
    try:
        hash(tok)
    except TypeError:
        return id(tok)
    return hash(tok)


def _edge_geometric_key(edge: Any, tolerance_mm: float = 1e-4) -> tuple[Any, ...] | None:
    """Hashable key from edge endpoint geometry (mm, order-independent).

    SolidWorks returns vertex coordinates in meters; keys round in mm at
    ``tolerance_mm``. Returns ``None`` when vertex/point accessors fail.
    """
    start = _call_or_value(edge, "GetStartVertex")
    end = _call_or_value(edge, "GetEndVertex")
    if start is None or end is None:
        return None

    start_point = _call_or_value(start, "GetPoint") or _call_or_value(start, "Point")
    end_point = _call_or_value(end, "GetPoint") or _call_or_value(end, "Point")
    if start_point is None or end_point is None:
        return None

    try:
        scale = 1000.0 / tolerance_mm
        s = tuple(round(float(c) * scale) for c in start_point[:3])
        e = tuple(round(float(c) * scale) for c in end_point[:3])
    except (TypeError, IndexError, ValueError):
        return None

    return tuple(sorted([s, e]))


def resolve_face(
    model: Any,
    face_ref: str,
    operation_id: str,
    operation_kind: str,
    ir_body_to_tree: dict[str, str] | None = None,
) -> Any:
    """Resolve ``body_X.face.<top|bottom|front|back|left|right>`` to an ``IFace2``."""
    try:
        body_ir_id, face_semantic = parse_face_ref(face_ref)
    except SolidWorksError as exc:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=str(exc),
        ) from exc

    target_body: Any = None
    all_bodies: list[Any] = []
    with suppress(Exception):
        all_bodies = list(model.GetBodies2(0, True) or [])

    if ir_body_to_tree:
        sw_body_name = ir_body_to_tree.get(body_ir_id)
        if sw_body_name:
            for b in all_bodies:
                try:
                    if str(b.Name) == sw_body_name:
                        target_body = b
                        break
                except Exception:
                    continue

    if target_body is None and all_bodies:
        target_body = all_bodies[0]

    if target_body is None:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(f"No solid body found in document to resolve face ref '{face_ref}'."),
        )

    faces: list[Any] = []
    with suppress(Exception):
        faces = list(target_body.GetFaces() or [])

    if not faces:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(f"Body has no faces; cannot resolve face ref '{face_ref}'."),
        )

    axis, sign = _FACE_DIRECTION_NORMAL[face_semantic]

    best_face: Any = None
    best_score: float = float("-inf")

    for face in faces:
        try:
            normal = face.Normal
            if normal is None or len(normal) < 3:
                continue
            score = float(normal[axis]) * sign
            if score > best_score:
                best_score = score
                best_face = face
        except Exception:
            continue

    if best_face is None:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(f"Could not determine '{face_semantic}' face for ref '{face_ref}'."),
        )

    return best_face


def resolve_edge(
    model: Any,
    edge_ref: str,
    operation_id: str,
    operation_kind: str,
    ir_body_to_tree: dict[str, str] | None = None,
) -> Any:
    """Resolve ``body_X.edge.<dir_a>_<dir_b>`` to an ``IEdge``.

    Both directions are resolved to ``IFace2`` via :func:`resolve_face`, then
    edges from each face are collected with ``IFace2.GetEdges`` (via
    :func:`_call_or_value` for materialized-tuple bindings).

    **Primary identity:** :func:`_edge_geometric_key` — endpoint coordinates
    reconcile distinct COM proxies for the same underlying edge.

    **Secondary:** :func:`_edge_identity_token` when both lists share an
    ``_oleobj_`` (common in unit tests).

    **Fallback:** :func:`_edge_borders_resolved_faces` via
    ``GetTwoAdjacentFaces2`` and outward normal equality.
    """
    match = EDGE_REF_RE.match(edge_ref)
    if match is None:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=f"Cannot parse edge reference '{edge_ref}'.",
        )
    body_ir_id = match.group(1)
    dir_a = match.group(2)
    dir_b = match.group(3)
    if dir_a == dir_b:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(f"Invalid edge ref '{edge_ref}': face directions must differ."),
        )

    face_a = f"{body_ir_id}.face.{dir_a}"
    face_b = f"{body_ir_id}.face.{dir_b}"
    f1 = resolve_face(model, face_a, operation_id, operation_kind, ir_body_to_tree)
    f2 = resolve_face(model, face_b, operation_id, operation_kind, ir_body_to_tree)

    edges1: list[Any] = []
    edges2: list[Any] = []
    try:
        edges1 = _coerce_edges(_call_or_value(f1, "GetEdges") or [])
    except _COM_READ_ERRORS as exc:
        logger.debug(
            "Face %s has no usable GetEdges accessor (%s); edge resolution may fail",
            face_a,
            type(exc).__name__,
        )
    try:
        edges2 = _coerce_edges(_call_or_value(f2, "GetEdges") or [])
    except _COM_READ_ERRORS as exc:
        logger.debug(
            "Face %s has no usable GetEdges accessor (%s); edge resolution may fail",
            face_b,
            type(exc).__name__,
        )

    keys1: dict[tuple[Any, ...], Any] = {}
    for edge in edges1:
        gkey = _edge_geometric_key(edge)
        if gkey is not None:
            keys1[gkey] = edge

    geo_matches: list[Any] = []
    for edge in edges2:
        gkey = _edge_geometric_key(edge)
        if gkey is not None and gkey in keys1:
            geo_matches.append(edge)

    if len(geo_matches) == 1:
        return geo_matches[0]
    if len(geo_matches) > 1:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(
                f"Ambiguous edge for ref '{edge_ref}': {len(geo_matches)} geometrically "
                f"matching edges between '{face_a}' and '{face_b}'."
            ),
        )

    tokens1 = {_edge_token_key(e) for e in edges1}
    shared_by_token: dict[int, Any] = {}
    for e in edges2:
        tok = _edge_token_key(e)
        if tok in tokens1:
            shared_by_token[tok] = e

    n = len(shared_by_token)
    if n == 1:
        return next(iter(shared_by_token.values()))

    adj_on_f1 = [e for e in edges1 if _edge_borders_resolved_faces(e, f1, f2)]
    adj_on_f2 = [e for e in edges2 if _edge_borders_resolved_faces(e, f1, f2)]

    if n == 0:
        if len(adj_on_f1) == 1:
            return adj_on_f1[0]
        if len(adj_on_f2) == 1:
            return adj_on_f2[0]
        if len(adj_on_f1) == 0 and len(adj_on_f2) == 0:
            raise SolidWorksFeatureCreationError(
                operation_id=operation_id,
                operation_kind=operation_kind,
                sw_error_message=(
                    f"No shared edge between faces '{face_a}' and '{face_b}' for ref '{edge_ref}'."
                ),
            )
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(
                f"Ambiguous edge for ref '{edge_ref}': {len(adj_on_f1)} edge(s) on "
                f"'{face_a}' and {len(adj_on_f2)} on '{face_b}' border both faces "
                f"(adjacency fallback)."
            ),
        )

    narrowed = {
        t: e for t, e in shared_by_token.items() if _edge_borders_resolved_faces(e, f1, f2)
    }
    if len(narrowed) == 1:
        return next(iter(narrowed.values()))
    raise SolidWorksFeatureCreationError(
        operation_id=operation_id,
        operation_kind=operation_kind,
        sw_error_message=(
            f"Ambiguous edge for ref '{edge_ref}': {n} distinct shared edges "
            f"between '{face_a}' and '{face_b}'."
        ),
    )


def select_face_ref(
    model: Any,
    face_ref: str,
    operation_id: str,
    operation_kind: str,
    builder_ctx: Any | None = None,
    append: bool = False,
    mark: int = 0,
) -> None:
    """Resolve a face ref to ``IFace2`` and select it via ``IFace2.Select4``.

    ``mark`` is accepted for API parity with ``select_face`` / ``SelectByID2``;
    ``Select4`` has no selection-mark slot and ignores it.
    """
    _ = int(mark)
    face = resolve_face(
        model,
        face_ref,
        operation_id,
        operation_kind,
        _ir_body_map(builder_ctx),
    )
    try:
        ok = bool(
            face.Select4(
                bool(append),
                null_dispatch(),
            )
        )
    except Exception as exc:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=f"IFace2.Select4 raised: {exc}",
        ) from exc
    if not ok:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(f"IFace2.Select4 returned False for face ref '{face_ref}'."),
        )


def select_edge_ref(
    model: Any,
    edge_ref: str,
    operation_id: str,
    operation_kind: str,
    builder_ctx: Any | None = None,
    append: bool = False,
    mark: int = 0,
) -> None:
    """Resolve an edge ref to ``IEdge`` and select it via ``IEdge.Select4``."""
    _ = int(mark)
    edge = resolve_edge(
        model,
        edge_ref,
        operation_id,
        operation_kind,
        _ir_body_map(builder_ctx),
    )
    try:
        ok = bool(
            edge.Select4(
                bool(append),
                null_dispatch(),
            )
        )
    except Exception as exc:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=f"IEdge.Select4 raised: {exc}",
        ) from exc
    if not ok:
        raise SolidWorksFeatureCreationError(
            operation_id=operation_id,
            operation_kind=operation_kind,
            sw_error_message=(f"IEdge.Select4 returned False for edge ref '{edge_ref}'."),
        )


__all__ = [
    "EDGE_REF_RE",
    "FACE_REF_RE",
    "is_semantic_edge_ref",
    "is_semantic_face_ref",
    "parse_face_ref",
    "resolve_edge",
    "resolve_face",
    "select_edge_ref",
    "select_face_ref",
]
