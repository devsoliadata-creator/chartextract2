"""Native-pixel measurement targets and audit for Agent 03's dense review."""

from __future__ import annotations

import math
import pathlib

import cv2


def _finite_pair(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        pair = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return pair if all(math.isfinite(number) for number in pair) else None


def _bounded_box(values, width, height, padding=12):
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        x0, y0, x1, y1 = map(float, values)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)) or x0 >= x1 or y0 >= y1:
        return None
    box = [max(0, math.floor(x0 - padding)), max(0, math.floor(y0 - padding)),
           min(width, math.ceil(x1 + padding)), min(height, math.ceil(y1 + padding))]
    return box if box[0] < box[2] and box[1] < box[3] else None


def build_targets(check, extraction, spec, out_dir):
    """Make source crops for Agent 02 undercoverage leads, or grouped unresolved slots.

    Slot predictions are never promoted to measurements. They only locate a
    native region for Agent 03 to inspect independently.
    """
    out_dir = pathlib.Path(out_dir)
    panel_bbox = spec.get("panel_bbox") or []
    if len(panel_bbox) != 4:
        raise ValueError("Dense measurement requires a four-coordinate native panel bbox")
    px0, py0, px1, py1 = map(int, panel_bbox)
    width, height = px1 - px0, py1 - py0
    if width <= 0 or height <= 0:
        raise ValueError("Dense measurement requires positive native panel dimensions")
    eligible = {
        str(series.get("label")): series for series in extraction.get("series") or []
        if series.get("extract") is True and str(series.get("gas") or "").upper() == "CO2"
    }
    leads = []
    for request in (check or {}).get("recovery_requests") or []:
        label = str(request.get("series_label") or "")
        norm = request.get("bbox_norm") or []
        if label not in eligible or len(norm) != 4:
            continue
        try:
            raw = [float(norm[0]) * width, float(norm[1]) * height,
                   float(norm[2]) * width, float(norm[3]) * height]
        except (TypeError, ValueError):
            continue
        box = _bounded_box(raw, width, height)
        if box is not None:
            leads.append((label, box, str(request.get("reason") or ""), "agent02_recovery_request"))

    # Agent 02 requests and unresolved slots are complementary. The latter can
    # lie outside broad low-pressure requests, as in the middle of chart 04.
    # Slot predictions locate native search windows; they are not centers.
    groups = {}
    for slot in ((extraction.get("unresolved_slots") or {}).get("slots") or []):
        label = str(slot.get("series_label") or "")
        center = _finite_pair([slot.get("column_px"), slot.get("predicted_y_px")])
        if label in eligible and center is not None:
            groups.setdefault((label, slot.get("region_index")), []).append(center)
    for (label, region), centers in groups.items():
        uncovered = [center for center in centers if not any(
            prior_label == label and box[0] <= center[0] <= box[2]
            and box[1] <= center[1] <= box[3]
            for prior_label, box, _, _ in leads
        )]
        if not uncovered:
            continue
        xs, ys = zip(*uncovered)
        box = _bounded_box([min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10],
                           width, height, padding=18)
        if box is not None:
            leads.append((label, box,
                          f"{len(uncovered)} unresolved search slots in region {region}",
                          "unresolved_slot_group"))

    source = cv2.imread(str(spec.get("image") or "")) if leads else None
    if leads and source is None:
        raise FileNotFoundError("Dense measurement cannot read the unchanged source image")
    panel = source[py0:py1, px0:px1] if source is not None else None
    if leads and panel.shape[:2] != (height, width):
        raise ValueError("Dense measurement panel bbox exceeds the source image")
    targets = []
    for label, box, hint, lead_source in leads:
        number = len(targets) + 1
        native_crop = None
        native_scale = 1
        if panel is not None:
            x0, y0, x1, y1 = box
            crop = panel[y0:y1, x0:x1]
            native_scale = max(1, min(3, int(1500 / max(crop.shape[:2]))))
            if native_scale > 1:
                crop = cv2.resize(crop, None, fx=native_scale, fy=native_scale,
                                  interpolation=cv2.INTER_NEAREST)
            native_crop = f"measurement_target_{number:02d}_native.png"
            out_dir.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(out_dir / native_crop), crop):
                raise OSError(f"Cannot save native measurement crop: {out_dir / native_crop}")
        candidate_rows = []
        for point in eligible[label].get("points") or []:
            center = _finite_pair(point.get("px"))
            if center and box[0] <= center[0] <= box[2] and box[1] <= center[1] <= box[3]:
                candidate_rows.append({"point_id": point.get("point_id"),
                                       "source_pixel": [round(center[0], 3), round(center[1], 3)]})
        targets.append({
            "target_id": f"dense:{number:02d}", "series_label": label,
            "source_bbox_px": box, "native_crop": native_crop, "native_scale": native_scale,
            "lead_source": lead_source, "source_hint": hint,
            "candidate_rows": candidate_rows,
        })
    return {"schema_version": "1.0", "panel_size_px": [width, height], "targets": targets}


def validate_measurements(answer, target_manifest, candidate):
    """Check that target centers are native, finite, and linked to point edits.

    This validates authorship and accounting, not whether the source truly has
    a marker at the reported pixel. That remains Agent 03's visual judgment.
    """
    targets = target_manifest.get("targets") or []
    if not targets:
        return []
    width, height = target_manifest["panel_size_px"]
    records = answer.get("dense_measurements")
    if not isinstance(records, list):
        return ["dense_measurements is missing; measure native centers for every target"]
    by_id = {str(record.get("target_id")): record for record in records if isinstance(record, dict)}
    problems = []
    if len(by_id) != len(records):
        problems.append("dense_measurements contains a duplicate or malformed target")
    expected = {target["target_id"] for target in targets}
    if set(by_id) != expected:
        problems.append("dense_measurements target IDs must equal " + ", ".join(sorted(expected)))
    points = {
        str(point.get("point_id")): (str(series.get("label")), _finite_pair(point.get("px")))
        for series in candidate.get("series") or [] for point in series.get("points") or []
        if point.get("point_id")
    }
    operations = {str(op.get("operation_id")): op for op in answer.get("operations") or []
                  if isinstance(op, dict) and op.get("operation_id")}
    deleted_ids = {str(op.get("point_id")) for op in operations.values() if op.get("action") == "delete"}
    for target in targets:
        target_id = target["target_id"]
        record = by_id.get(target_id)
        if not isinstance(record, dict):
            continue
        label = target["series_label"]
        box = target["source_bbox_px"]
        if record.get("series_label") != label or record.get("source_bbox_px") != box:
            problems.append(f"{target_id}: series or native ROI differs from measurement target")
        status = record.get("status")
        centers = record.get("centers")
        if not isinstance(centers, list):
            problems.append(f"{target_id}: centers must be an array")
            continue
        if status == "unresolved":
            problems.append(f"{target_id}: generic unresolved result; measure source centers or give a native contradiction")
        elif status == "measured" and not centers:
            problems.append(f"{target_id}: measured result has no native centers")
        elif status == "source_disagrees" and len(str(record.get("native_evidence") or "")) < 60:
            problems.append(f"{target_id}: source disagreement needs specific native evidence")
        elif status == "measured" and len(str(record.get("native_evidence") or "")) < 30:
            problems.append(f"{target_id}: describe the native marker sequence and line comparison")
        try:
            lower = int(record.get("source_count_lower"))
            upper = record.get("source_count_upper")
            upper = int(upper) if upper is not None else None
            unresolved = int(record.get("unresolved_count"))
            if lower < 0 or unresolved < 0 or (upper is not None and upper < lower):
                raise ValueError
            if lower > len(centers) + unresolved or (upper is not None and upper < len(centers)):
                problems.append(f"{target_id}: measured centers and unresolved count do not reconcile with source bounds")
            if status == "measured" and (upper is None or unresolved):
                problems.append(f"{target_id}: measured inventory still has unbounded or unlocalized instances")
        except (TypeError, ValueError):
            problems.append(f"{target_id}: source count bounds are invalid")
        linked_operations = set()
        linked_points = set()
        for index, item in enumerate(centers, start=1):
            if not isinstance(item, dict):
                problems.append(f"{target_id}: center {index} is malformed")
                continue
            center = _finite_pair(item.get("source_pixel"))
            if center is None or not (box[0] <= center[0] <= box[2] and box[1] <= center[1] <= box[3]):
                problems.append(f"{target_id}: center {index} is outside its native ROI")
                continue
            try:
                uncertainty = float(item.get("uncertainty_px"))
                if not math.isfinite(uncertainty) or uncertainty <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{target_id}: center {index} needs positive native-pixel uncertainty")
            if not str(item.get("source_evidence") or "").strip():
                problems.append(f"{target_id}: center {index} needs native source evidence")
            point_id, operation_id = item.get("point_id"), item.get("operation_id")
            if bool(point_id) == bool(operation_id):
                problems.append(f"{target_id}: center {index} must link one point ID or one operation ID")
            elif point_id:
                prior = points.get(str(point_id))
                if prior is None or prior[0] != label or str(point_id) in deleted_ids:
                    problems.append(f"{target_id}: center {index} links an absent, deleted, or wrong-series candidate")
                elif prior[1] is None or math.hypot(prior[1][0] - center[0], prior[1][1] - center[1]) > 6:
                    problems.append(f"{target_id}: center {index} is over 6 px from retained candidate; author a move")
                if str(point_id) in linked_points:
                    problems.append(f"{target_id}: candidate {point_id} is linked to multiple centers")
                linked_points.add(str(point_id))
            else:
                operation = operations.get(str(operation_id))
                if not operation or operation.get("action") not in {"add", "move", "reassign"}:
                    problems.append(f"{target_id}: center {index} has no positional edit {operation_id}")
                else:
                    operation_label = operation.get("series_label") or (points.get(str(operation.get("point_id"))) or (None,))[0]
                    if operation_label != label:
                        problems.append(f"{target_id}: edit {operation_id} has a different series")
                    edited = _finite_pair([operation.get("x_norm"), operation.get("y_norm")])
                    if edited is None or math.hypot(edited[0] * width - center[0],
                                                   edited[1] * height - center[1]) > 2:
                        problems.append(f"{target_id}: edit {operation_id} differs from measured center by over 2 px")
                    if str(operation_id) in linked_operations:
                        problems.append(f"{target_id}: edit {operation_id} is linked to multiple centers")
                    linked_operations.add(str(operation_id))
        for operation in operations.values():
            if operation.get("action") not in {"add", "move", "reassign"}:
                continue
            operation_label = operation.get("series_label") or (points.get(str(operation.get("point_id"))) or (None,))[0]
            if operation_label != label:
                continue
            edited = _finite_pair([operation.get("x_norm"), operation.get("y_norm")])
            if edited is None:
                continue
            x, y = edited[0] * width, edited[1] * height
            if box[0] <= x <= box[2] and box[1] <= y <= box[3] and str(operation["operation_id"]) not in linked_operations:
                problems.append(f"{target_id}: positional edit {operation['operation_id']} lacks a measured-center link")
    return problems
