"""Evidence-bound handling for crowded, low-pressure marker regions.

This module deliberately separates a *slot hypothesis* from a coordinate.  A
shared pressure column can make a missing marker worth reviewing, but it can
never create a CSV point: only a local color/template core may do that.
"""
from __future__ import annotations

from collections import Counter
import copy
import json
import math
import pathlib
import cv2
import numpy as np
from scipy import ndimage
from src.tools import marker_assignment


def validate_complex_regions(regions, frame_px, fx, x_limits, panel_shape):
    """Convert accepted panel-normalized boxes to plot pixels.

    A region described as low-pressure must actually be confined to the low-x
    part of the calibrated plot.  This catches broad horizontal bands (often
    the N2 traces) that happen to sit low in image coordinates.
    """
    left, top, right, bottom = map(float, frame_px)
    height, width = panel_shape[:2]
    xmin, xmax = map(float, x_limits)
    span = max(1e-9, xmax - xmin)
    accepted, rejected = [], []
    for raw in regions or []:
        box = raw.get("bbox_norm") or []
        if len(box) != 4:
            rejected.append({"region": raw, "reason": "missing_bbox_norm"}); continue
        x0, y0, x1, y1 = map(float, box)
        xa, xb = sorted((x0 * width, x1 * width)); ya, yb = sorted((y0 * height, y1 * height))
        clipped = (max(left, xa), max(top, ya), min(right, xb), min(bottom, yb))
        if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
            rejected.append({"region": raw, "reason": "outside_calibrated_plot"}); continue
        kind = str(raw.get("kind") or "").lower()
        # Read legacy saved runs, but new Agent 02 responses are schema-bound
        # to `kind`; this is not a free-text classification path.
        if not kind and raw.get("reason"):
            kind = "dense_markers" if "dense" in str(raw["reason"]).lower() else "annotation"
        if kind not in {"dense_markers", "overlapping_markers", "thick_connecting_lines", "crossing", "inset", "annotation"}:
            rejected.append({"region": raw, "reason": "invalid_region_kind"}); continue
        data_x0, data_x1 = sorted((float(fx(clipped[0])), float(fx(clipped[2]))))
        if (raw.get("low_pressure") or "low-pressure" in str(raw.get("reason") or "").lower()) and (data_x1 - xmin) / span > 0.35:
            rejected.append({"region": raw, "reason": "low_pressure_box_extends_beyond_low_x_domain",
                             "data_x": [data_x0, data_x1]}); continue
        accepted.append({"bbox_px": [round(v, 2) for v in clipped], "data_x": [data_x0, data_x1], **raw})
    return accepted, rejected


def infer_low_pressure_roi(
    frame_px,
    all_series,
    fraction=0.15,
    *,
    axis_scale="linear",
    axis_limits=None,
    slope=None,
    intercept=None,
):
    """Return a frame/axis-derived low-x strip spanning the full plot height.

    ``fraction`` is a fraction of the *data* domain when calibration details
    are supplied (including on logarithmic axes).  The pixel-fraction fallback
    preserves compatibility with direct callers that only know the frame.
    Existing marker y coordinates deliberately do not trim the strip: missing
    starts often sit above or below the already detected fan.
    """
    left, top, right, bottom = map(float, frame_px)
    fraction = min(0.5, max(0.01, float(fraction)))
    cut = left + fraction * (right - left)
    limits = list(axis_limits or [])
    strip_start, strip_end = None, None
    if len(limits) == 2 and slope not in (None, 0):
        low, high = sorted(float(value) for value in limits)
        if axis_scale == "log" and low > 0 and high > low:
            strip_start = low
            strip_end = low * (high / low) ** fraction
            start_transformed = float(np.log10(strip_start))
            end_transformed = float(np.log10(strip_end))
        else:
            # Axes whose frame extends below zero often include blank negative
            # padding. Start the search at zero so that padding does not use up
            # the crowded-strip width.
            strip_start = 0.0 if low <= 0 < high else low
            strip_end = strip_start + fraction * (high - strip_start)
            start_transformed, end_transformed = strip_start, strip_end
        start_px = (start_transformed - float(intercept or 0.0)) / float(slope)
        end_px = (end_transformed - float(intercept or 0.0)) / float(slope)
        if np.isfinite(start_px) and np.isfinite(end_px):
            strip_left, strip_right = sorted((
                min(right, max(left, start_px)),
                min(right, max(left, end_px)),
            ))
            if strip_right > strip_left:
                return [strip_left, top, strip_right, bottom]
    cut = left + fraction * (right - left)
    # Pixel-fraction fallback is conservative for callers without axes.
    return [left, top, cut, bottom]


def infer_pressure_columns(all_series, roi, radius):
    """Infer columns from clear native markers, not Agent 01's grid hypothesis."""
    xa, ya, xb, yb = roi
    hits = []
    for series in all_series:
        for x, y, confidence, overlap, sure in series.get("pts", []):
            if xa <= x <= xb and ya <= y <= yb and sure and confidence >= .65:
                hits.append((x, series["idx"]))
    hits.sort()
    columns = []
    for x, owner in hits:
        if not columns or x - columns[-1]["x"] > max(2.0, .8 * radius):
            columns.append({"x": float(x), "owners": {owner}})
        else:
            col = columns[-1]; col["owners"].add(owner); col["x"] = (col["x"] + x) / 2
    return [col for col in columns if len(col["owners"]) >= 2]


def _local_core(lab, color, tol, roi, column_x, y_guess, radius, line_width=1.0, native_mask=None):
    xa, ya, xb, yb = map(int, roi)
    search = max(4, int(round(1.5 * radius)))
    x0, x1 = max(xa, int(column_x - search)), min(xb, int(column_x + search + 1))
    y0, y1 = max(ya, int(y_guess - 2.5 * radius)), min(yb, int(y_guess + 2.5 * radius + 1))
    if x0 >= x1 or y0 >= y1: return None
    if native_mask is None:
        dist = np.linalg.norm(lab[y0:y1, x0:x1] - color[None, None, :], axis=2)
        mask = (dist < tol).astype(np.uint8)
    else:
        mask = (native_mask[y0:y1, x0:x1] > 0).astype(np.uint8)
    # Lines are thin; a marker has a distance-transform core.  This is the
    # line-suppressed residual used for resolution, not a fitted trajectory.
    dt = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    _, peak, _, loc = cv2.minMaxLoc(dt)
    # A thick connector can have a real distance-transform peak.  It is only
    # marker evidence when its core exceeds the declared/observed line width.
    if peak < max(1.0, .32 * radius, 0.75 * line_width): return None
    return float(x0 + loc[0]), float(y0 + loc[1]), float(peak)


def resolve_dense_region(all_series, lab, roi, radius, native_mask=None, geometry_indices=None):
    """Jointly resolve native-supported cores and record every unsupported slot.

    The one-series/one-column constraint is enforced by considering each pair
    once.  Existing measured points win; no coordinate is interpolated.
    """
    columns = infer_pressure_columns(all_series, roi, radius)
    resolved, unresolved = [], []
    if not columns:
        # Sparse or single-series dense regions cannot safely yield a shared
        # marker column. They still need source-first review coverage rather
        # than disappearing from the dense evidence ledger.
        for series in all_series:
            unresolved.append({
                "series_label": series["spec"].get("label"),
                "series_index": series["idx"],
                "roi_px": [round(float(value), 2) for value in roi],
                "state": "unresolved",
                "reason": "dense_region_has_no_confirmed_shared_columns",
            })
        return resolved, unresolved, columns
    for column_index, column in enumerate(columns):
        for series in all_series:
            points = series.get("pts", [])
            series_radius = max(2.0, float(series.get("r") or radius))
            native_points = [
                point for point in points
                if len(point) >= 5 and bool(point[4]) and float(point[2]) >= 0.65
            ]
            # Trajectory continuity supplies a local search window only. At a
            # boundary, one-sided native anchors may guide that search; the
            # native core check below must still confirm the marker.
            left = [p for p in native_points if p[0] < column["x"]]
            right = [p for p in native_points if p[0] > column["x"]]
            if left and right:
                anchor_left = max(left, key=lambda p: p[0])
                anchor_right = min(right, key=lambda p: p[0])
                if anchor_left[0] == anchor_right[0]:
                    continue
                trajectory_slope = (anchor_right[1] - anchor_left[1]) / (anchor_right[0] - anchor_left[0])
                y_guess = anchor_left[1] + trajectory_slope * (column["x"] - anchor_left[0])
                trajectory_mode = "interpolated_native_anchors"
            elif right:
                right_ordered = sorted(right, key=lambda p: p[0])
                anchors = right_ordered[:1] + [p for p in right_ordered[1:] if p[0] != right_ordered[0][0]][:1]
                anchor = anchors[0]
                anchor_gap = abs(float(anchor[0]) - float(column["x"]))
                if len(anchors) == 2:
                    trajectory_slope = (anchors[1][1] - anchors[0][1]) / (anchors[1][0] - anchors[0][0])
                    y_guess = anchor[1] + trajectory_slope * (column["x"] - anchor[0])
                else:
                    y_guess = anchor[1]
                trajectory_mode = "one_sided_native_search"
            elif left:
                left_ordered = sorted(left, key=lambda p: p[0], reverse=True)
                anchors = left_ordered[:1] + [p for p in left_ordered[1:] if p[0] != left_ordered[0][0]][:1]
                anchor = anchors[0]
                anchor_gap = abs(float(anchor[0]) - float(column["x"]))
                if len(anchors) == 2:
                    trajectory_slope = (anchors[0][1] - anchors[1][1]) / (anchors[0][0] - anchors[1][0])
                    y_guess = anchor[1] + trajectory_slope * (column["x"] - anchor[0])
                else:
                    y_guess = anchor[1]
                trajectory_mode = "one_sided_native_search"
            else:
                unresolved.append({"series_label": series["spec"].get("label"), "series_index": series["idx"], "column_index": column_index, "column_px": round(column["x"], 2), "state": "unresolved", "reason": "no_native_trajectory_anchors"}); continue
            if trajectory_mode == "one_sided_native_search":
                neighbor_gaps = [
                    abs(float(a[0]) - float(b[0]))
                    for a, b in zip(sorted(native_points, key=lambda p: p[0]), sorted(native_points, key=lambda p: p[0])[1:])
                ]
                typical_gap = float(np.median(neighbor_gaps)) if neighbor_gaps else 0.0
                search_span = max(4.0 * series_radius, 2.0 * typical_gap)
                if anchor_gap > search_span:
                    unresolved.append({
                        "series_label": series["spec"].get("label"), "series_index": series["idx"],
                        "column_index": column_index, "column_px": round(column["x"], 2),
                        "predicted_y_px": round(float(y_guess), 2), "state": "unresolved",
                        "reason": "one_sided_search_outside_native_anchor_span",
                    })
                    continue
            if not (roi[1] <= y_guess <= roi[3]):
                continue
            # Column membership is two-dimensional.  A real marker stacked
            # above or below this prediction does not occupy the predicted
            # marker slot merely because it shares an x coordinate.
            if any(np.hypot(p[0] - column["x"], p[1] - y_guess) < .8 * series_radius for p in points):
                continue
            core = _local_core(
                lab,
                series["col"],
                series["tol"],
                roi,
                column["x"],
                y_guess,
                series_radius,
                series.get("line_width", 1.0),
                native_mask=native_mask if series["idx"] in set(geometry_indices or ()) else None,
            )
            slot = {"series_label": series["spec"].get("label"), "series_index": series["idx"],
                    "column_index": column_index, "column_px": round(column["x"], 2),
                    "predicted_y_px": round(float(y_guess), 2), "state": "unresolved",
                    "trajectory_mode": trajectory_mode}
            if core is None:
                slot["reason"] = "no_native_marker_core_after_line_suppression"; unresolved.append(slot); continue
            x, y, core_radius = core
            # A core must be genuinely marker-sized and inside the ROI. This
            # rejects a one-pixel colored curve even when continuity predicts it.
            if core_radius < .45 * series_radius:
                slot["reason"] = "line_like_residual_not_marker"; unresolved.append(slot); continue
            resolved.append({"series_index": series["idx"], "column_index": column_index,
                             "px": [round(x, 2), round(y, 2)], "confidence": .65,
                             "core_radius": round(core_radius, 2)})
    # One indistinguishable native core cannot establish multiple direct
    # observations. Preserve competing owners as an unresolved hypothesis for
    # review, rather than silently deleting alternatives or emitting two rows.
    by_location = {}
    for item in resolved:
        by_location.setdefault((item["column_index"], round(item["px"][0]), round(item["px"][1])), []).append(item)
    for hypothesis_number, items in enumerate(by_location.values(), start=1):
        if len(items) > 1:
            labels = [next((series["spec"].get("label") for series in all_series
                            if series["idx"] == item["series_index"]), str(item["series_index"]))
                      for item in items]
            group_id = f"dense-core-{items[0]['column_index']}-{hypothesis_number}"
            for item, label in zip(items, labels):
                unresolved.append({
                    "series_label": label,
                    "series_index": item["series_index"],
                    "hypothesis_group_id": group_id,
                    "candidate_series_indices": [candidate["series_index"] for candidate in items],
                    "candidate_series_labels": labels,
                    "column_index": item["column_index"],
                    "column_px": item["px"][0],
                    "predicted_y_px": item["px"][1],
                    "candidate_px": item["px"],
                    "state": "unresolved",
                    "reason": "indistinguishable_native_core_multiple_series_hypotheses",
                })
            resolved = [item for item in resolved if item not in items]
    # Defensive duplicate prevention: a series owns at most one point per slot.
    unique = {}
    for item in resolved:
        key = (item["series_index"], item["column_index"])
        if key not in unique or item["core_radius"] > unique[key]["core_radius"]: unique[key] = item
    return list(unique.values()), unresolved, columns


def unresolved_summary(slots):
    counts = Counter(slot["series_label"] for slot in slots)
    return {"total": len(slots), "by_series": dict(sorted(counts.items())), "slots": slots}


def _band_color_mask(hsv, color_hex):
    """Find a series' native hue, including the darker ink of a fitted band."""
    try:
        color = str(color_hex).lstrip("#")
        red, green, blue = (int(color[offset:offset + 2], 16) for offset in (0, 2, 4))
    except (ValueError, IndexError):
        return None, False
    target = cv2.cvtColor(np.uint8([[[blue, green, red]]]), cv2.COLOR_BGR2HSV)[0, 0]
    hue, saturation, value = map(int, target)
    dark = saturation < 70 and value < 110
    if dark:
        return hsv[:, :, 2] <= 75, True
    if saturation < 55:
        return None, False
    distance = np.abs(hsv[:, :, 0].astype(np.int16) - hue)
    distance = np.minimum(distance, 180 - distance)
    hue_tolerance = 20 if 40 <= hue <= 90 else 10
    return ((distance <= hue_tolerance) & (hsv[:, :, 1] >= max(35, int(0.2 * saturation)))
            & (hsv[:, :, 2] >= 25)), False


def _band_path(component, x0, y0, radius, *, vertical):
    """Median native ink cross-sections, thinned by distance along the band."""
    ys, xs = np.nonzero(component)
    if not len(xs):
        return []
    raw = []
    if vertical:
        for row in range(int(ys.max()), int(ys.min()) - 1, -1):
            cross = np.flatnonzero(component[row])
            if len(cross):
                raw.append((float(x0 + np.median(cross)), float(y0 + row),
                            max(1.5, min(12.0, (float(np.ptp(cross)) + 1.0) / 2.0))))
    else:
        for column in range(int(xs.min()), int(xs.max()) + 1):
            cross = np.flatnonzero(component[:, column])
            if len(cross):
                raw.append((float(x0 + column), float(y0 + np.median(cross)),
                            max(1.5, min(12.0, (float(np.ptp(cross)) + 1.0) / 2.0))))
    if not raw:
        return []
    spacing = max(4.0, 2.0 * float(radius))
    selected = [raw[0]]
    for sample in raw[1:]:
        if math.dist(sample[:2], selected[-1][:2]) >= spacing:
            selected.append(sample)
    if math.dist(raw[-1][:2], selected[-1][:2]) >= spacing / 2.0:
        selected.append(raw[-1])
    return selected


def sample_continuous_bands(image, series_styles, regions, frame_px, mask_bboxes=()):
    """Digitize source-backed band centres as trace samples, not marker centres.

    Typed crowded regions define the search area. An elongated connected
    component of a series' own ink supplies every emitted coordinate. Isolated
    glyphs, thin grid/axis strokes, and same-color series are not sampled.
    """
    if image is None or not regions:
        return [], {"samples": 0, "bands": [], "skipped": "no native image or crowded regions"}
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    frame = list(map(float, frame_px))
    if len(frame) != 4:
        return [], {"samples": 0, "bands": [], "skipped": "no calibrated frame"}
    colors = Counter(str(item.get("color_hex") or "").lower() for item in series_styles if item.get("extract", True))
    samples, bands = [], []
    source_regions = [region for region in regions if region.get("kind") == "thick_connecting_lines"]
    if not source_regions:
        source_regions = [region for region in regions if region.get("kind") == "dense_markers"]
    for region_index, region in enumerate(source_regions):
        box = region.get("bbox_px") or []
        if len(box) != 4:
            continue
        for style in series_styles:
            if not style.get("extract", True):
                continue
            label = str(style.get("label") or "")
            color_hex = str(style.get("color_hex") or "").lower()
            if not label or colors[color_hex] != 1:
                continue
            color_mask, dark = _band_color_mask(hsv, color_hex)
            if color_mask is None:
                continue
            edge_margin = 4 if dark else 1
            x0 = max(0, int(math.ceil(max(float(box[0]), frame[0] + edge_margin))))
            y0 = max(0, int(math.ceil(max(float(box[1]), frame[1] + 1))))
            x1 = min(width, int(math.floor(min(float(box[2]), frame[2] - 1))) + 1)
            y1 = min(height, int(math.floor(min(float(box[3]), frame[3] - edge_margin))) + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = color_mask[y0:y1, x0:x1].astype(np.uint8)
            if dark:
                # A black series shares its color with axes and marker
                # outlines. Agent 02's per-x loading bands limit the native
                # search to this series without supplying coordinates.
                y_bands = style.get("y_bands") or []
                models = style.get("axis_models") or {}
                if not y_bands or "x" not in models or "y" not in models:
                    continue
                xx = np.arange(x0, x1, dtype=float)[None, :]
                yy = np.arange(y0, y1, dtype=float)[:, None]
                x_model, y_model = models["x"], models["y"]
                x_value = float(x_model["slope"]) * xx + float(x_model["intercept"])
                y_value = float(y_model["slope"]) * yy + float(y_model["intercept"])
                if x_model.get("scale") == "log":
                    x_value = 10.0 ** x_value
                if y_model.get("scale") == "log":
                    y_value = 10.0 ** y_value
                tolerance_y = float(style.get("marker_radius_px") or 5.0) * abs(float(y_model["slope"]))
                allowed = np.zeros_like(crop, dtype=bool)
                for band in y_bands:
                    allowed |= ((float(band["x_from"]) <= x_value) & (x_value <= float(band["x_to"]))
                                & (float(band["y_min"]) - tolerance_y <= y_value)
                                & (y_value <= float(band["y_max"]) + tolerance_y))
                crop &= allowed.astype(np.uint8)
            for mask in mask_bboxes:
                if len(mask) != 4:
                    continue
                xa, ya, xb, yb = map(float, mask)
                row_start, row_end = max(0, int(math.floor(ya)) - y0), min(y1 - y0, int(math.ceil(yb)) - y0)
                col_start, col_end = max(0, int(math.floor(xa)) - x0), min(x1 - x0, int(math.ceil(xb)) - x0)
                if row_start < row_end and col_start < col_end:
                    crop[row_start:row_end, col_start:col_end] = 0
            # OpenCV's connectedComponentsWithStats can terminate the Windows
            # process on small, narrow cropped masks. SciPy gives the same
            # eight-connected regions without crossing the Python boundary.
            labels, count = ndimage.label(crop, structure=np.ones((3, 3), dtype=np.uint8))
            component_slices = ndimage.find_objects(labels)
            component_areas = np.bincount(labels.ravel(), minlength=count + 1)
            radius = max(2.0, float(style.get("marker_radius_px") or 5.0))
            for component_id in range(1, count + 1):
                bounds = component_slices[component_id - 1]
                if bounds is None:
                    continue
                row_slice, column_slice = bounds
                bx, by = column_slice.start, row_slice.start
                bw = column_slice.stop - bx
                bh = row_slice.stop - by
                area = int(component_areas[component_id])
                if (max(bw, bh) < max(35.0, 6.0 * radius)
                        or min(bw, bh) < max(5.0, 1.2 * radius)
                        or area < max(100.0, 6.0 * radius * radius)):
                    continue
                component = labels[bounds] == component_id
                path = _band_path(component, x0 + bx, y0 + by, radius, vertical=bh >= 1.4 * bw)
                if len(path) < 3:
                    continue
                band_id = f"region-{region_index + 1}:{label}:{component_id}"
                bands.append({"band_id": band_id, "series_label": label,
                              "bbox_px": [x0 + bx, y0 + by, x0 + bx + bw, y0 + by + bh],
                              "native_ink_pixels": area, "sample_count": len(path),
                              "marker_radius_px": radius})
                for order, (px, py, uncertainty) in enumerate(path):
                    samples.append({"band_id": band_id, "series_label": label,
                                    "source_pixel": [round(px, 2), round(py, 2)],
                                    "uncertainty_px": round(uncertainty, 2), "order": order})
    # Overlapping typed ROIs may see the same connected band. Keep one sample
    # per native pixel neighbourhood without reducing the sampled cadence.
    unique = []
    for sample in samples:
        if not any(other["series_label"] == sample["series_label"]
                   and math.dist(other["source_pixel"], sample["source_pixel"]) < 2.0
                   for other in unique):
            unique.append(sample)
    return unique, {"samples": len(unique), "bands": bands,
                    "method": "native HSV component cross-section medians at marker-width arc spacing"}


def supporting_trace_exclusion_masks(stage, spec, image_shape):
    """Return masks shared by native and legacy supporting trace proposals."""
    calibration = stage.get("calibration") or {}
    regions = calibration.get("complex_regions_accepted") or []
    exclusion_masks = list(spec.get("mask_bboxes") or [])
    # Never present legend/annotation/ignored source pixels as native trace
    # evidence. These are masks for sampling only; the original image remains
    # available in review tiles unchanged.
    if len(spec.get("legend_bbox") or []) == 4:
        exclusion_masks.append(spec["legend_bbox"])
    def region_bbox(region):
        bbox = region.get("bbox_px") or []
        if len(bbox) == 4:
            return bbox
        normalized = region.get("bbox_norm") or []
        if len(normalized) == 4 and image_shape is not None:
            height, width = image_shape[:2]
            x0, y0, x1, y1 = (float(value) for value in normalized)
            return [x0 * width, y0 * height, x1 * width, y1 * height]
        return None

    for region in [*(spec.get("complex_regions") or []), *(regions or [])]:
        bbox = region_bbox(region)
        if str(region.get("kind") or "").lower() == "inset" and bbox is not None:
            exclusion_masks.append(bbox)
    for region in spec.get("region_strategies") or []:
        if str(region.get("strategy") or "").lower() == "ignore" and len(region.get("bbox_px") or []) == 4:
            exclusion_masks.append(region["bbox_px"])
    return exclusion_masks


def build_supporting_traces(stage, spec, image_path):
    """Build ordered native band evidence without changing candidate markers."""
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    calibration = stage.get("calibration") or {}
    regions = calibration.get("complex_regions_accepted") or []
    styles_by_label = {str(item.get("label")): item for item in spec.get("series") or []}
    styles = [{"label": series.get("label"), "color_hex": styles_by_label.get(series.get("label"), {}).get("color_hex"),
               "marker_radius_px": series.get("marker_radius_px"), "extract": series.get("extract", True),
               "y_bands": styles_by_label.get(series.get("label"), {}).get("y_bands"),
               "axis_models": calibration.get("axis_models")}
              for series in stage.get("series") or []]
    exclusion_masks = supporting_trace_exclusion_masks(stage, spec, image.shape if image is not None else None)
    samples, audit = sample_continuous_bands(
        image, styles, regions, calibration.get("frame_px") or spec.get("panel_bbox") or [],
        exclusion_masks,
    )
    audit["excluded_mask_bboxes"] = exclusion_masks
    artifact = {"schema_version": "1.0", "role": "supporting_only",
                "authority": "native_raster_supporting_evidence",
                "source_image": pathlib.Path(image_path).name,
                "coordinate_space": "panel_pixels",
                "panel_shape_px": [int(image.shape[1]), int(image.shape[0])] if image is not None else None,
                "crop_transform": {"panel_origin_px": [0, 0], "scale": 1}, "traces": []}
    if not samples:
        audit.update(trace_count=0, trace_sample_count=0, samples_by_series={})
        return artifact, audit
    axis_models = calibration.get("axis_models") or {}
    styles_by_label = {str(item.get("label")): item for item in spec.get("series") or []}
    grouped = {}
    for sample in samples:
        grouped.setdefault((sample["band_id"], sample["series_label"]), []).append(sample)
    for trace_number, ((band_id, label), samples_for_trace) in enumerate(grouped.items(), start=1):
        def value(axis, pixel):
            model = axis_models[axis]
            transformed = float(model["slope"]) * pixel + float(model["intercept"])
            return 10.0 ** transformed if model.get("scale") == "log" else transformed
        points = []
        for order, sample in enumerate(sorted(samples_for_trace, key=lambda item: item.get("order", 0))):
            px, py = sample["source_pixel"]
            points.append({"order": order, "px": [px, py], "x": round(value("x", px), 4),
                           "y": round(value("y", py), 4), "uncertainty_px": sample["uncertainty_px"],
                           "native_support": "band_cross_section_median"})
        artifact["traces"].append({"trace_id": f"native-band-{trace_number:03d}", "segment_id": band_id,
                                   "series_name": label, "color_hex": styles_by_label.get(label, {}).get("color_hex"),
                                   "role": "supporting_only", "ordered_points": points})
    audit.update(trace_count=len(artifact["traces"]),
                 trace_sample_count=sum(len(trace["ordered_points"]) for trace in artifact["traces"]),
                 samples_by_series=dict(Counter(trace["series_name"] for trace in artifact["traces"] for _ in trace["ordered_points"])),
                 role="supporting_only")
    return artifact, audit


def add_line_samples(stage, spec, image_path):
    """Compatibility helper: dense lines are supporting evidence, never rows."""
    artifact, audit = build_supporting_traces(stage, spec, image_path)
    audit["staged_line_samples"] = 0
    audit["supporting_trace_artifact"] = artifact
    return copy.deepcopy(stage), audit


def move_supporting_point_proposals(stage, artifact, spec=None):
    """Move legacy curve/grid proposals out of a candidate point inventory.

    They retain their series and source identity as review evidence, but a
    predicted curve crossing can never become an experimental marker row.
    Only authored segment IDs and trace order establish a drawable path.
    """
    moved, excluded = [], 0
    shape = artifact.get("panel_shape_px") or []
    if len(shape) == 2:
        width, height = shape
        image_shape = (height, width)
    else:
        image_shape = None
    masks = supporting_trace_exclusion_masks(stage, spec or {}, image_shape)
    frame = (stage.get("calibration") or {}).get("frame_px") or [0, 0, width if len(shape) == 2 else float("inf"), height if len(shape) == 2 else float("inf")]
    try:
        frame = [float(value) for value in frame]
    except (TypeError, ValueError):
        frame = [0, 0, width if len(shape) == 2 else float("inf"), height if len(shape) == 2 else float("inf")]
    if len(frame) != 4 or not all(math.isfinite(value) for value in frame) or frame[0] >= frame[2] or frame[1] >= frame[3]:
        frame = [0, 0, width if len(shape) == 2 else float("inf"), height if len(shape) == 2 else float("inf")]
    def allowed(px):
        if not isinstance(px, (list, tuple)) or len(px) != 2:
            return False
        try:
            x, y = float(px[0]), float(px[1])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(x) or not math.isfinite(y) or not (frame[0] <= x <= frame[2] and frame[1] <= y <= frame[3]):
            return False
        for box in masks:
            try:
                x0, y0, x1, y1 = (float(value) for value in box)
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(value) for value in (x0, y0, x1, y1)) and min(x0, x1) <= x < max(x0, x1) and min(y0, y1) <= y < max(y0, y1):
                return False
        return True
    for series in stage.get("series") or []:
        retained = []
        grouped, isolated = {}, []
        for point in series.get("points") or []:
            source = str(point.get("source") or "")
            if source not in {"curve", "grid"}:
                retained.append(point)
                continue
            px = point.get("px") or []
            if not allowed(px):
                excluded += 1
                continue
            segment_id = point.get("segment_id") or point.get("trace_segment_id")
            trace_order = point.get("trace_order")
            if segment_id is None or not isinstance(trace_order, (int, float)) or not math.isfinite(float(trace_order)):
                isolated.append((source, point))
                continue
            key = (source, point.get("trace_id"), segment_id, point.get("branch"))
            grouped.setdefault(key, []).append(point)
        series["points"] = retained
        series["n_points"] = len(retained)
        for (source, trace_id, segment_id, branch), points in grouped.items():
            ordered = [{"order": point["trace_order"], "trace_order": point["trace_order"], "px": list(point["px"]),
                        "x": point.get("x"), "y": point.get("y"), "uncertainty_px": point.get("uncertainty_px"),
                        "native_support": "python_supporting_proposal"} for point in points]
            artifact["traces"].append({"trace_id": trace_id, "segment_id": segment_id,
                                       "series_name": series.get("label"), "source_identity": source,
                                       "role": "supporting_only", "branch": branch, "ordered_points": ordered})
            moved.extend(points)
        for source, point in isolated:
            artifact["traces"].append({"trace_id": point.get("trace_id"), "segment_id": None,
                                       "series_name": series.get("label"), "source_identity": source,
                                       "role": "supporting_only", "branch": point.get("branch"),
                                       "ordered_points": [{"px": list(point["px"]), "x": point.get("x"),
                                                           "y": point.get("y"), "uncertainty_px": point.get("uncertainty_px"),
                                                           "native_support": "python_supporting_proposal"}]})
            moved.append(point)
    return {"moved_candidate_rows": len(moved), "excluded_candidate_rows": excluded,
            "moved_by_source": dict(Counter(str(point.get("source")) for point in moved))}


def write_evidence(image, extraction, spec, out_dir):
    """Write Agent 03 inspection views for the calibrated dense ROI."""
    info = (extraction.get("calibration") or {}).get("dense_region_resolver") or {}
    roi = info.get("low_pressure_strip_px") or info.get("roi_px")
    if roi and isinstance(roi[0], (list, tuple)):
        roi = roi[0]  # a representative evidence tile; manifest contains all typed ROIs
    if not roi:
        return {}
    xa, ya, xb, yb = map(int, roi)
    panel = image.copy()
    native = panel[ya:yb, xa:xb]
    if native.size == 0:
        return {}
    out_dir = pathlib.Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    native_path = out_dir / "dense_roi_native.png"
    cv2.imwrite(str(native_path), cv2.resize(native, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST))
    # Morphological opening removes broad marker cores; residual is primarily
    # thin fitted lines, making the distinction inspectable without claiming a point.
    residual = cv2.absdiff(native, cv2.morphologyEx(native, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)))
    residual_path = out_dir / "dense_roi_line_suppressed.png"
    cv2.imwrite(str(residual_path), cv2.resize(residual, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
    likelihood = native.copy()
    for series in extraction.get("series", []):
        for point in series.get("points", []):
            px = point.get("px") or []
            if len(px) == 2 and xa <= px[0] <= xb and ya <= px[1] <= yb:
                cv2.circle(likelihood, (int(px[0] - xa), int(px[1] - ya)), 4, (0, 255, 255), 1)
    likelihood_path = out_dir / "dense_roi_series_likelihood.png"
    cv2.imwrite(str(likelihood_path), cv2.resize(likelihood, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
    assignments = likelihood.copy()
    for slot in (extraction.get("unresolved_slots") or {}).get("slots", []):
        column_px = slot.get("column_px")
        predicted_y_px = slot.get("predicted_y_px")
        # Some unresolved slots intentionally have no y coordinate.  In
        # particular, a trajectory that is not bracketed by native anchors is
        # evidence of a missing assignment, not evidence of a pixel location.
        # Keep it in the ledger but do not invent a marker for this image.
        if not isinstance(column_px, (int, float)) or not isinstance(
            predicted_y_px, (int, float)
        ):
            continue
        if not np.isfinite(column_px) or not np.isfinite(predicted_y_px):
            continue
        x, y = int(column_px - xa), int(predicted_y_px - ya)
        cv2.drawMarker(assignments, (x, y), (0, 0, 255), cv2.MARKER_TILTED_CROSS, 7, 1)
    assignments_path = out_dir / "dense_roi_proposed_assignments.png"
    cv2.imwrite(str(assignments_path), cv2.resize(assignments, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
    evidence = {"native": native_path.name, "line_suppressed": residual_path.name,
                "series_likelihood": likelihood_path.name, "proposed_assignments": assignments_path.name}

    # Preserve untouched native tiles for every disputed color-ambiguous
    # candidate.  Agent 03 can inspect these without mistaking a Python ring
    # or score annotation for coordinate evidence.
    audit = (extraction.get("calibration") or {}).get("series_assignment_audit") or {}
    disputed = [
        candidate for candidate in audit.get("candidates", [])
        if candidate.get("action") in {"unresolved", "reassigned"}
    ]
    native_tiles = []
    score_overlay = panel.copy()
    for index, candidate in enumerate(disputed[:80], start=1):
        px = candidate.get("px") or []
        if len(px) != 2:
            continue
        cx, cy = map(int, map(round, px))
        xa, xb = max(0, cx - 32), min(panel.shape[1], cx + 33)
        ya, yb = max(0, cy - 32), min(panel.shape[0], cy + 33)
        tile = panel[ya:yb, xa:xb]
        if tile.size == 0:
            continue
        tile_path = out_dir / f"ambiguity_{index:03d}_native.png"
        cv2.imwrite(str(tile_path), cv2.resize(tile, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST))
        native_tiles.append(tile_path.name)
        color = (0, 0, 255) if candidate.get("action") == "unresolved" else (0, 165, 255)
        cv2.circle(score_overlay, (cx, cy), 8, color, 1)
        cv2.putText(score_overlay, str(index), (cx + 8, cy - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    if native_tiles:
        score_path = out_dir / "ambiguity_assignment_scores.png"
        cv2.imwrite(str(score_path), score_overlay)
        diagnostics_path = out_dir / "assignment_diagnostics.json"
        diagnostics_path.write_text(json.dumps(audit, indent=1), encoding="utf-8")
        evidence["ambiguity_native_tiles"] = native_tiles
        evidence["ambiguity_assignment_scores"] = score_path.name
        evidence["assignment_diagnostics"] = diagnostics_path.name
    return evidence
