"""Apply Agent 03/04 extraction edits to a staged extraction.

The agents choose which source instances to add, move, remove, or reassign.
This module performs only mechanical bookkeeping and calibrated pixel-to-axis
conversion so every model-authored change is retained in an audit trail.
"""

from __future__ import annotations

import copy
import math

from src.models import naming
from src.tools.extract import pixel_to_axis


def compact_rows(rows):
    """Return the coordinate/identity fields needed by an extraction agent.

    Per-series constants (marker shape, color, species, export flag) are left out - the agent
    has them from the structure - so a 17-series chart with hundreds of rows fits in a prompt.
    """
    keys = ("point_id", "series_name", "x", "y", "source_pixel_x", "source_pixel_y", "confidence", "evidence_type")
    compact = []
    for row in rows:
        item = {key: row.get(key) for key in keys}
        assigned = [part.split("=", 1)[1] for part in str(row.get("notes") or "").split("; ") if part.startswith("assigned_by=")]
        if assigned:
            item["assigned_by"] = assigned[0]
        compact.append(item)
    return compact


def rows_table(rows) -> str:
    """``compact_rows`` as CSV text for a prompt: one header line, one short line per point."""
    import csv
    import io

    compact = compact_rows(rows)
    columns = ["point_id", "series_name", "x", "y", "source_pixel_x", "source_pixel_y", "confidence", "evidence_type",
               "assigned_by"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(compact)
    return buffer.getvalue().rstrip("\n")


def _point_index(extraction):
    return {
        str(point.get("point_id")): (series, point)
        for series in extraction.get("series", [])
        for point in series.get("points", [])
        if point.get("point_id")
    }


def _point_matches(extraction, point_id):
    """Return all points carrying an ID so duplicate identities are never guessed."""
    return [
        (series, point)
        for series in extraction.get("series", [])
        for point in series.get("points", [])
        if str(point.get("point_id") or "") == point_id and point_id
    ]


def _series_index(extraction):
    return {naming.series_label(series.get("label")): series for series in extraction.get("series", [])}


def _series_matches(extraction, label):
    return [
        series for series in extraction.get("series", [])
        if naming.series_label(series.get("label")) == label and label
    ]


def _is_agent03(actor):
    return str(actor or "").lower() in {"agent03", "agent03_review_points"} or str(actor or "").lower().startswith("agent03_")


def _eligible_series(series):
    gas = "".join(
        char for char in str(series.get("gas") or "").upper() if char.isalnum()
    )
    return bool(series.get("extract", False)) and gas == "CO2"


def _pixel(operation, width, height):
    x_norm, y_norm = operation.get("x_norm"), operation.get("y_norm")
    if x_norm is None or y_norm is None:
        return None
    if (isinstance(x_norm, bool) or isinstance(y_norm, bool)
            or not isinstance(x_norm, (int, float)) or not isinstance(y_norm, (int, float))):
        return None
    try:
        x_norm, y_norm = float(x_norm), float(y_norm)
    except (TypeError, ValueError):
        return None
    if (not math.isfinite(x_norm) or not math.isfinite(y_norm)
            or not 0 <= x_norm <= 1 or not 0 <= y_norm <= 1):
        return None
    # Preserve the agent's calibrated coordinate. Rounding to 0.01 px here
    # can turn an explicitly chosen axis origin into a nonzero data value.
    return [x_norm * width, y_norm * height]


def _inside_plot(px, extraction):
    """Return whether a panel-local pixel lies in the calibrated plot frame."""
    frame = (extraction.get("calibration") or {}).get("frame_px")
    if not frame or len(frame) != 4:
        return True
    left, top, right, bottom = (float(value) for value in frame)
    return left <= px[0] <= right and top <= px[1] <= bottom


def _position_problem(px, extraction, spec):
    if not _inside_plot(px, extraction):
        return "position is outside calibrated plot frame"
    if any(float(left) <= px[0] <= float(right) and float(top) <= px[1] <= float(bottom)
           for left, top, right, bottom in spec.get("mask_bboxes") or []):
        return "position is inside an excluded source region"
    return None


def _position_warnings(px, extraction, spec):
    warnings = []
    if not _inside_plot(px, extraction):
        warnings.append("position_outside_calibrated_plot_frame")
    if any(float(left) <= px[0] <= float(right) and float(top) <= px[1] <= float(bottom)
           for left, top, right, bottom in spec.get("mask_bboxes") or []):
        warnings.append("position_inside_excluded_source_region")
    return warnings


def _marker_radius(series, extraction):
    """Best available native marker radius in panel pixels.

    Stage edits operate in panel pixels, so the series' measured radius is a
    better collision scale than an axis-unit tolerance.  The conservative
    fallback keeps hand-authored/legacy extractions safe when the field is
    absent without making the check effectively global.
    """
    try:
        value = series.get("marker_radius_px")
        if value is None:
            value = (extraction.get("calibration") or {}).get("marker_radius_px")
        return max(1.0, float(value)) if value is not None else 4.0
    except (TypeError, ValueError):
        return 4.0


def _same_series_collision(series, px, extraction, *, exclude=None):
    """Find a preserved point too close to a model-authored target pixel.

    A point inside one marker radius is flagged as a possible duplicate.
    Callers retain explicitly evidenced overlap instances and reject an
    undeclared collision. ``exclude`` prevents a move/reassign from colliding
    with the point itself.
    """
    target_radius = _marker_radius(series, extraction)
    for existing in series.get("points", []):
        if existing is exclude:
            continue
        existing_px = existing.get("px")
        if not existing_px or existing_px[0] is None or existing_px[1] is None:
            continue
        try:
            distance = math.hypot(float(px[0]) - float(existing_px[0]), float(px[1]) - float(existing_px[1]))
        except (TypeError, ValueError):
            continue
        existing_radius = _marker_radius(series, extraction)
        collision_radius = max(target_radius, existing_radius)
        if distance <= collision_radius:
            return {
                "point_id": existing.get("point_id"),
                "source_pixel": [round(float(existing_px[0]), 2), round(float(existing_px[1]), 2)],
                "distance_px": round(float(distance), 3),
                "collision_radius_px": round(float(collision_radius), 3),
                "rule": "same_series_within_marker_radius",
            }
    return None


def _collision_audit(record, collision):
    return {
        **record,
        "status": "not_applied",
        "detail": "same_series_marker_collision",
        "collision_prevented": True,
        "collision": collision,
    }


def _overlap_override(operation):
    """An explicit, evidenced overlap may represent a second logical marker."""
    return operation.get("overlap") is True and _positional_support(operation) is not None


def _collision_override_audit(record, collision):
    return {
        **record,
        "collision_override": True,
        "collision": collision,
        "detail": "explicit_native_supported_overlap",
    }


def _positional_support(operation):
    """Require native support and a positive uncertainty for a changed position."""
    kind = operation.get("evidence_kind")
    source = operation.get("source_evidence")
    reference = operation.get("evidence_ref")
    if not all(isinstance(value, str) for value in (kind, source, reference)):
        return None
    source, reference = source.strip(), reference.strip()
    try:
        raw_uncertainty = operation.get("uncertainty_px")
        if isinstance(raw_uncertainty, bool) or not isinstance(raw_uncertainty, (int, float)):
            return None
        uncertainty = float(raw_uncertainty)
    except (TypeError, ValueError):
        return None
    if (kind not in {"native_visible", "partial_marker", "estimated"} or not source or not reference
            or not math.isfinite(uncertainty) or uncertainty <= 0):
        return None
    return kind, source, reference, uncertainty


def _nonpositional_support(operation, action):
    """Validate evidence citations for operations that do not change a position."""
    kind = operation.get("evidence_kind")
    source = operation.get("source_evidence")
    reference = operation.get("evidence_ref")
    if not all(isinstance(value, str) for value in (kind, source, reference)):
        return None
    source, reference = source.strip(), reference.strip()
    allowed = {"native_visible", "partial_marker", "native_absence"}
    if action == "reassign":
        allowed.discard("native_absence")
        allowed.add("estimated")
    if kind not in allowed or not source or not reference or operation.get("uncertainty_px") is not None:
        return None
    return kind, source, reference


def _audit_uncertainty(value):
    """Keep invalid direct-call values inspectable without writing non-JSON NaN."""
    if isinstance(value, bool) or value is None:
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    return numeric if math.isfinite(numeric) else str(value)


def _operation_evidence(operation):
    """Copy operation evidence without erasing details from legacy point records."""
    values = {}
    for key in ("evidence_kind", "uncertainty_px", "evidence_ref", "source_evidence"):
        if key in operation and not (key == "uncertainty_px" and operation[key] is None):
            values[key] = _json_safe(operation[key])
    return values


def _json_safe(value):
    """Make malformed direct-call operation values safe to retain in JSON audits."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _operation_list(response):
    """Return the authored operation entries, including a malformed container as an entry."""
    if not isinstance(response, dict):
        return [response]
    operations = response.get("operations", [])
    if not isinstance(operations, list):
        return [{"__malformed_operations_container__": operations}]
    return operations


def _evidence_warning(operation, action, has_position):
    support = _positional_support(operation) if has_position else _nonpositional_support(operation, action)
    return [] if support is not None else ["missing_or_invalid_native_evidence"]


def _valid_confidence_uncertainty(operation):
    value = operation.get("uncertainty_px")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _apply_point_evidence(point, operation, series, extraction):
    point.update(_operation_evidence(operation))
    kind = operation.get("evidence_kind")
    uncertainty = _valid_confidence_uncertainty(operation)
    if kind in {"partial_marker", "estimated"} and uncertainty is not None:
        score = _added_point_confidence(kind, uncertainty, series, extraction)
        try:
            point["confidence"] = min(float(point["confidence"]), score)
        except (KeyError, TypeError, ValueError):
            point["confidence"] = score


def _added_point_confidence(evidence_kind, uncertainty_px, series, extraction):
    """Conservative confidence from the cited positional uncertainty and marker scale.

    This is only the legacy display score used by downstream plots. The native
    evidence kind and raw uncertainty remain attached to the point and export.
    """
    marker_radius = _marker_radius(series, extraction)
    score = min(0.79, 1.0 / (1.0 + uncertainty_px / marker_radius))
    if evidence_kind == "partial_marker":
        score = min(score, 0.64)
    elif evidence_kind == "estimated":
        # No visible centre was found at all: always bucket to "low" confidence,
        # regardless of how tight the cited pixel uncertainty is.
        score = min(score, 0.5)
    return round(max(0.25, score), 3)


def _recalculate(extraction):
    calibration = extraction.get("calibration") or {}
    for series in extraction.get("series", []):
        for point in series.get("points", []):
            px = point.get("px")
            if not px or px[0] is None or px[1] is None:
                continue
            point["x"] = round(float(pixel_to_axis(calibration, "x", float(px[0]))), 4)
            point["y"] = round(float(pixel_to_axis(calibration, "y", float(px[1]))), 4)
        series["points"].sort(
            key=lambda point: (
                math.inf if point.get("x") is None else float(point["x"]),
                math.inf if point.get("y") is None else float(point["y"]),
            )
        )
        series["n_points"] = len(series["points"])
        xs = [point["x"] for point in series["points"] if point.get("x") is not None]
        ys = [point["y"] for point in series["points"] if point.get("y") is not None]
        series["x_min"], series["x_max"] = (min(xs), max(xs)) if xs else (None, None)
        series["y_min"], series["y_max"] = (min(ys), max(ys)) if ys else (None, None)
    return extraction


def apply_agent_edits(extraction, spec, response, actor):
    """Apply authored operations and return extraction plus an audit of checks and outcomes."""
    result = copy.deepcopy(extraction)
    bbox = spec.get("panel_bbox") or [0, 0, 0, 0]
    width, height = float(bbox[2] - bbox[0]), float(bbox[3] - bbox[1])
    audit = []
    agent03 = _is_agent03(actor)
    operations = _operation_list(response)

    for index, operation in enumerate(operations):
        if isinstance(operation, dict) and "__malformed_operations_container__" in operation:
            audit.append({
                "operation_index": index,
                "operation_id": f"{actor}-{index + 1}",
                "action": None,
                "actor": actor,
                "status": "not_applied",
                "detail": "operations must be an array",
                "malformed_operation": True,
                "authored_operation": _json_safe(operation["__malformed_operations_container__"]),
            })
            continue
        if not isinstance(operation, dict):
            audit.append({
                "operation_index": index,
                "operation_id": f"{actor}-{index + 1}",
                "action": None,
                "actor": actor,
                "status": "not_applied",
                "detail": "operation must be an object",
                "malformed_operation": True,
                "authored_operation": _json_safe(operation),
            })
            continue
        action = str(operation.get("action") or "")
        point_id = str(operation.get("point_id") or "")
        target_label = naming.series_label(operation.get("series_label"))
        target_matches = _series_matches(result, target_label)
        target_series = target_matches[0] if len(target_matches) == 1 else None
        point_matches = _point_matches(result, point_id)
        source = point_matches[0] if len(point_matches) == 1 else None
        record = {
            "operation_index": index,
            "operation_id": _json_safe(operation.get("operation_id") or f"{actor}-{index + 1}"),
            "action": action,
            "point_id": point_id or None,
            "series_label": target_label or None,
            "reason": _json_safe(operation.get("reason", "")),
            "source_evidence": _json_safe(operation.get("source_evidence", "")),
            "evidence_kind": _json_safe(operation.get("evidence_kind")),
            "uncertainty_px": _audit_uncertainty(operation.get("uncertainty_px")),
            "evidence_ref": _json_safe(operation.get("evidence_ref")),
            "actor": actor,
        }

        if action not in {"add", "move", "delete", "reassign"}:
            audit.append({
                **record,
                "status": "not_applied",
                "detail": "unsupported action" if action else "missing action",
                "malformed_operation": True,
                "authored_operation": _json_safe(operation),
            })
            continue

        if target_label and len(target_matches) > 1:
            audit.append({
                **record,
                "status": "not_applied",
                "detail": "ambiguous target series label",
            })
            continue

        if point_id and len(point_matches) > 1:
            audit.append({
                **record,
                "status": "not_applied",
                "detail": "ambiguous point_id",
            })
            continue

        if target_series is not None and not _eligible_series(target_series):
            audit.append(
                {
                    **record,
                    "status": "not_applied",
                    "detail": "target series is excluded by CO2 policy",
                }
            )
            continue

        has_position = action in {"add", "move"} or (
            action == "reassign"
            and operation.get("x_norm") is not None
            and operation.get("y_norm") is not None
        )
        support = _positional_support(operation) if has_position else None
        coordinates_supplied = operation.get("x_norm") is not None or operation.get("y_norm") is not None
        if action == "reassign" and coordinates_supplied and not (
                operation.get("x_norm") is not None and operation.get("y_norm") is not None):
            audit.append({
                **record,
                "status": "not_applied",
                "detail": "incomplete normalized position",
                "malformed_operation": True,
                "authored_operation": _json_safe(operation),
            })
            continue
        if action in {"add", "move", "reassign"} and not agent03:
            valid_support = support if has_position else _nonpositional_support(operation, action)
            if valid_support is None:
                audit.append({
                    **record,
                    "status": "not_applied",
                    "detail": "operation requires native evidence, an evidence reference, and valid positional uncertainty",
                })
                continue

        if action == "add":
            px = _pixel(operation, width, height)
            if len(target_matches) != 1:
                audit.append(
                    {
                        **record,
                        "status": "not_applied",
                        "detail": "unknown target series",
                    }
                )
                continue
            if px is None:
                audit.append({
                    **record,
                    "status": "not_applied",
                    "detail": "position must contain finite normalized coordinates in [0, 1]",
                    "malformed_operation": True,
                    "authored_operation": _json_safe(operation),
                })
                continue
            position_problem = _position_problem(px, result, spec)
            if position_problem:
                if not agent03:
                    audit.append({**record, "status": "not_applied", "detail": position_problem})
                    continue
            collision = _same_series_collision(target_series, px, result)
            if collision is not None and not agent03:
                if not _overlap_override(operation):
                    audit.append(_collision_audit(record, collision))
                    continue
                collision_override = _collision_override_audit(record, collision)
            elif collision is not None:
                collision_override = {}
            else:
                collision_override = {}
            evidence_kind = operation.get("evidence_kind")
            source_evidence = operation.get("source_evidence")
            evidence_ref = operation.get("evidence_ref")
            uncertainty_px = _valid_confidence_uncertainty(operation)
            warnings = _evidence_warning(operation, action, True) if agent03 else []
            if agent03:
                warnings.extend(_position_warnings(px, result, spec))
                if collision is not None:
                    warnings.append("same_series_marker_collision")
            target_series.setdefault("points", []).append(
                {
                    "x": None,
                    "y": None,
                    "px": px,
                    "confidence": (
                        _added_point_confidence(support[0], support[3], target_series, result)
                        if support is not None else 0.25
                    ),
                    "overlap_flag": bool(operation.get("overlap", False)),
                    "assigned_by": actor,
                    "source": actor,
                    "evidence_kind": _json_safe(evidence_kind),
                    "uncertainty_px": uncertainty_px,
                    "evidence_ref": _json_safe(evidence_ref),
                    "source_evidence": _json_safe(source_evidence),
                }
            )
            if agent03:
                audit.append({**record, "status": "applied", "source_pixel": px,
                              **({"warnings": list(dict.fromkeys(warnings))} if warnings else {}),
                              **({"collision": collision} if collision is not None else {})})
            else:
                audit.append({**record, **collision_override, "status": "applied", "source_pixel": px})
            continue

        if source is None:
            audit.append(
                {**record, "status": "not_applied",
                 "detail": "unknown point_id" if not point_id or not point_matches else "unresolvable point_id"}
            )
            continue
        source_series, point = source

        if action == "delete":
            source_series["points"].remove(point)
            audit.append({**record, "status": "applied"})
        elif action == "move":
            px = _pixel(operation, width, height)
            if px is None:
                audit.append({
                    **record,
                    "status": "not_applied",
                    "detail": "position must contain finite normalized coordinates in [0, 1]",
                    "malformed_operation": True,
                    "authored_operation": _json_safe(operation),
                })
                continue
            position_problem = _position_problem(px, result, spec)
            if position_problem:
                if not agent03:
                    audit.append({**record, "status": "not_applied", "detail": position_problem})
                    continue
            collision = _same_series_collision(source_series, px, result, exclude=point)
            if collision is not None and not agent03:
                if not _overlap_override(operation):
                    audit.append(_collision_audit(record, collision))
                    continue
                collision_override = _collision_override_audit(record, collision)
            elif collision is not None:
                collision_override = {}
            else:
                collision_override = {}
            point["px"] = px
            if operation.get("overlap") is True:
                point["overlap_flag"] = True
            point["assigned_by"] = actor
            point["source"] = actor
            _apply_point_evidence(point, operation, source_series, result)
            if agent03:
                warnings = _evidence_warning(operation, action, True) + _position_warnings(px, result, spec)
                if collision is not None:
                    warnings.append("same_series_marker_collision")
                audit.append({**record, "status": "applied", "source_pixel": px,
                              **({"warnings": list(dict.fromkeys(warnings))} if warnings else {}),
                              **({"collision": collision} if collision is not None else {})})
            else:
                audit.append({**record, **collision_override, "status": "applied", "source_pixel": px})
        elif action == "reassign":
            if target_series is None:
                audit.append(
                    {
                        **record,
                        "status": "not_applied",
                        "detail": "unknown target series",
                    }
                )
                continue
            px = _pixel(operation, width, height)
            if coordinates_supplied and px is None:
                audit.append({
                    **record,
                    "status": "not_applied",
                    "detail": "position must contain finite normalized coordinates in [0, 1]",
                    "malformed_operation": True,
                    "authored_operation": _json_safe(operation),
                })
                continue
            target_px = px if px is not None else point.get("px")
            position_problem = _position_problem(target_px, result, spec) if target_px is not None else None
            if position_problem:
                if not agent03:
                    audit.append({**record, "status": "not_applied", "detail": position_problem})
                    continue
            if target_px is not None:
                collision = _same_series_collision(target_series, target_px, result, exclude=point if source_series is target_series else None)
                if collision is not None and not agent03:
                    if not _overlap_override(operation):
                        audit.append(_collision_audit(record, collision))
                        continue
                    collision_override = _collision_override_audit(record, collision)
                elif collision is not None:
                    collision_override = {}
                else:
                    collision_override = {}
            else:
                collision_override = {}
            if source_series is not target_series:
                source_series["points"].remove(point)
                target_series.setdefault("points", []).append(point)
            if px is not None:
                point["px"] = px
            if operation.get("overlap") is True:
                point["overlap_flag"] = True
            if has_position:
                _apply_point_evidence(point, operation, target_series, result)
            else:
                point.update(_operation_evidence(operation))
            point["assigned_by"] = actor
            point["source"] = actor
            if agent03:
                warnings = _evidence_warning(operation, action, has_position)
                if target_px is not None:
                    warnings.extend(_position_warnings(target_px, result, spec))
                if collision is not None:
                    warnings.append("same_series_marker_collision")
                audit.append({**record, "status": "applied", "source_pixel": point.get("px"),
                              **({"warnings": list(dict.fromkeys(warnings))} if warnings else {}),
                              **({"collision": collision} if collision is not None else {})})
            else:
                audit.append({**record, **collision_override, "status": "applied", "source_pixel": point.get("px")})

    result = _recalculate(result)
    return result, {
        "schema_version": "1.0",
        "actor": actor,
        "requested": len(operations),
        "applied": sum(item["status"] == "applied" for item in audit),
        "warning_count": sum(len(item.get("warnings") or []) for item in audit),
        "operations": audit,
    }
