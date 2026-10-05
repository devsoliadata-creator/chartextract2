"""Dense-region resolver: ribbon tracking, template correlation and ink-mass counting.

Crowded isotherm regions (the low-pressure fan) defeat blob detection because
markers touch or overlap.  This module treats each series as a *ribbon* of its
own colour and walks along it:

1. the series colour is measured at its clean, isolated markers (right of the
   dense limit), and a colour mask is built with a tolerance wide enough to keep
   the anti-aliased ribbon connected;
2. marker radius, ink area and a binary glyph template come from those clean
   markers, the connecting-line width from the ink between them;
3. the ribbon is tracked leftward from the leftmost clean marker by
   arc-centroid stepping, re-centred on the cross-section every step, never
   heading rightward (an isotherm is single-valued in x), bridging gaps up to
   one marker spacing, and using existing candidate rows as waypoints only when
   they sit on the series' own ink;
4. along the path, ink excess over the line level and normalised template
   correlation are combined; local maxima are marker centres;
5. the ink mass along the path bounds the marker count; the shortfall against
   peaks plus existing rows is placed at the visible pressure setpoints on the
   path (``estimated_marker``), else evenly along the fused stretch.

Nothing here deletes rows.  ``on_ink_mask`` / ``point_on_ink`` are shared with
the stage builder, which flags every row that does not sit on its own series'
colour (``on_series_ink=false`` in notes) so the reviewers can see suspects.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

DEFAULTS = {
    "dense_ribbon_color_tolerance": 34.0,   # Lab units; the anti-aliased ribbon, not only marker cores
    "dense_ribbon_limit_fraction": 0.25,    # search left of this fraction of the x span (data units)
    "dense_ribbon_min_clean_markers": 3,
    "dense_ribbon_peak_score": 0.5,         # combined ink/template score for a measured centre
    "dense_ribbon_shape_score": 0.5,        # template correlation needed to call a peak shape-supported
    "on_ink_tolerance_lab": 34.0,
    "on_ink_area_fraction": 0.4,            # share of a 0.8 r disc that must carry the series colour
}


# ---------- shared helpers (also used by stage_artifacts) ----------


def lab_image(bgr):
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)


def lab_from_hex(value):
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return cv2.cvtColor(np.uint8([[[blue, green, red]]]), cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)


HUE_WINDOW_DEG = 18.0       # same-hue shades of a coloured series count as its ink
MIN_CHROMA = 25.0           # below this the colour is grey/black: use the full Lab distance only
SHADE_CHROMA_FRACTION = 0.4  # a shade must keep this share of the marker's chroma (excludes background)


def on_ink_mask(lab, colour, tolerance):
    """Boolean mask of pixels that carry the series colour.

    Pixels within ``tolerance`` Lab units always match.  For a coloured series the
    mask also accepts darker or lighter *shades* of the same hue: fused marker bands
    are rendered darker than isolated markers, so a pure Lab distance drops them.
    A shade matches when its chroma direction is within ``HUE_WINDOW_DEG`` of the
    marker colour and its chroma magnitude keeps ``SHADE_CHROMA_FRACTION`` of it.
    """
    colour = np.asarray(colour, dtype=np.float32)
    mask = np.linalg.norm(lab - colour[None, None, :], axis=2) < float(tolerance)
    ca, cb = float(colour[1]) - 128.0, float(colour[2]) - 128.0
    chroma = math.hypot(ca, cb)
    if chroma < MIN_CHROMA:
        return mask
    pa, pb = lab[..., 1] - 128.0, lab[..., 2] - 128.0
    pixel_chroma = np.hypot(pa, pb)
    cos_angle = (pa * ca + pb * cb) / np.maximum(pixel_chroma * chroma, 1e-6)
    shade = ((cos_angle >= math.cos(math.radians(HUE_WINDOW_DEG)))
             & (pixel_chroma >= SHADE_CHROMA_FRACTION * chroma)
             & (lab[..., 0] <= 235.0))
    return mask | shade


def disc_count(mask, cx, cy, radius):
    """Number of mask pixels inside the disc of ``radius`` around (cx, cy)."""
    height, width = mask.shape[:2]
    x0, x1 = max(0, int(cx - radius - 1)), min(width, int(cx + radius + 2))
    y0, y1 = max(0, int(cy - radius - 1)), min(height, int(cy + radius + 2))
    if x1 <= x0 or y1 <= y0:
        return 0
    sub = mask[y0:y1, x0:x1]
    gy, gx = np.mgrid[y0:y1, x0:x1]
    return int((((gx - cx) ** 2 + (gy - cy) ** 2) <= radius * radius)[sub].sum())


def marker_ink_reference(mask, points, radius):
    """Median ink in a 1.1 r disc at the series' confident points (open or filled glyphs)."""
    confident = [p for p in points if p.get("px") and p["px"][0] is not None
                 and float(p.get("confidence") or 0) >= 0.6]
    sample = confident or [p for p in points if p.get("px") and p["px"][0] is not None]
    if not sample:
        return 0.0
    return float(np.median([disc_count(mask, float(p["px"][0]), float(p["px"][1]), 1.1 * float(radius))
                            for p in sample]))


def point_on_ink(mask, px, radius, reference_area, area_fraction=DEFAULTS["on_ink_area_fraction"]):
    """Whether a point sits on its series' colour.

    The ink inside a 1.1 r disc must reach ``area_fraction`` of ``reference_area``,
    the ink a confident marker of the same series carries, so hollow glyphs are
    judged against hollow glyphs rather than against a filled disc.
    """
    if reference_area <= 0:
        return True
    return disc_count(mask, float(px[0]), float(px[1]), 1.1 * float(radius)) >= area_fraction * reference_area


# ---------- internals ----------


def _axis_maps(calibration):
    models = (calibration or {}).get("axis_models") or {}

    def to_value(axis, pixel):
        model = models.get(axis)
        if not model:
            return None
        value = model["slope"] * float(pixel) + model["intercept"]
        return 10 ** value if model.get("scale") == "log" else value

    def to_pixel(axis, value):
        model = models.get(axis)
        if not model or model["slope"] == 0:
            return None
        target = math.log10(value) if model.get("scale") == "log" else float(value)
        return (target - model["intercept"]) / model["slope"]

    return to_value, to_pixel


def _radial_profile(mask, cx, cy, rmax=24):
    height, width = mask.shape[:2]
    x0, x1 = max(0, int(cx - rmax - 1)), min(width, int(cx + rmax + 2))
    y0, y1 = max(0, int(cy - rmax - 1)), min(height, int(cy + rmax + 2))
    sub = mask[y0:y1, x0:x1]
    gy, gx = np.mgrid[y0:y1, x0:x1]
    dist = np.sqrt((gx - cx) ** 2 + (gy - cy) ** 2)
    profile = []
    for k in range(rmax):
        ring = (dist >= k) & (dist < k + 1)
        profile.append(float(sub[ring].mean()) if ring.any() else 0.0)
    return np.array(profile)


def _crop(mask, cx, cy, half):
    """Binary crop of ``mask`` centred on (cx, cy), zero padded, size 2*half+1."""
    height, width = mask.shape[:2]
    out = np.zeros((2 * half + 1, 2 * half + 1), np.float32)
    x0, y0 = int(round(cx)) - half, int(round(cy)) - half
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(width, x0 + 2 * half + 1), min(height, y0 + 2 * half + 1)
    if sx1 <= sx0 or sy1 <= sy0:
        return out
    out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = mask[sy0:sy1, sx0:sx1]
    return out


def _ncc(a, b):
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / denom) if denom > 1e-9 else 0.0


def _ahead_centroid(mask, p, d, step, half_angle, reach=1.0):
    height, width = mask.shape[:2]
    r_out = step * 1.8 * reach
    x0, x1 = max(0, int(p[0] - r_out - 1)), min(width, int(p[0] + r_out + 2))
    y0, y1 = max(0, int(p[1] - r_out - 1)), min(height, int(p[1] + r_out + 2))
    sub = mask[y0:y1, x0:x1]
    if not sub.any():
        return None
    gy, gx = np.mgrid[y0:y1, x0:x1]
    vx, vy = gx - p[0], gy - p[1]
    dist = np.sqrt(vx ** 2 + vy ** 2)
    ring = sub & (dist >= step * 0.8 * reach) & (dist <= r_out)
    if not ring.any():
        return None
    cosang = (vx * d[0] + vy * d[1]) / np.maximum(dist, 1e-6)
    ahead = ring & (cosang >= math.cos(math.radians(half_angle)))
    if not ahead.any():
        return None
    return np.array([float(gx[ahead].mean()), float(gy[ahead].mean())])


def _recenter(mask, q, d, radius):
    height, width = mask.shape[:2]
    normal = np.array([-d[1], d[0]])
    samples = np.arange(-2.0 * radius, 2.0 * radius + 0.5, 0.5)
    pts = q[None, :] + samples[:, None] * normal[None, :]
    xs, ys = np.round(pts[:, 0]).astype(int), np.round(pts[:, 1]).astype(int)
    ok = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    hit = np.zeros(len(samples), bool)
    hit[ok] = mask[ys[ok], xs[ok]]
    if not hit.any():
        return q
    idx = np.where(hit)[0]
    nearest = idx[np.argmin(np.abs(idx - len(samples) // 2))]
    lo = hi = nearest
    while lo - 1 >= 0 and hit[lo - 1]:
        lo -= 1
    while hi + 1 < len(samples) and hit[hi + 1]:
        hi += 1
    return q + samples[(lo + hi) // 2] * normal


def _track(mask, start, heading, radius, frame, stop_y, max_steps=4000):
    step = max(2.0, 0.6 * radius)
    p = np.array(start, dtype=float)
    d = np.array(heading, dtype=float)
    d /= max(np.linalg.norm(d), 1e-6)
    path = [p.copy()]
    for _ in range(max_steps):
        q = None
        for half_angle in (60.0, 90.0):
            q = _ahead_centroid(mask, p, d, step, half_angle)
            if q is not None:
                break
        if q is None:
            for reach in (2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0):
                q = _ahead_centroid(mask, p, d, step, 35.0 if reach <= 4 else 60.0, reach=reach)
                if q is not None:
                    break
        if q is None:
            break
        nd = q - p
        if np.linalg.norm(nd) < 0.3:
            break
        nd /= np.linalg.norm(nd)
        if float(nd @ d) < -0.2:
            break
        d = 0.5 * d + 0.5 * nd
        d /= np.linalg.norm(d)
        if d[0] > 0.25:
            break
        q = _recenter(mask, q, d, radius)
        p = q
        if len(path) > 25 and np.min(np.linalg.norm(np.array(path[:-20]) - p, axis=1)) < step:
            break
        path.append(p.copy())
        if p[0] <= frame[0] + 1 or p[1] >= frame[3] - 1 or p[1] <= frame[1] + 1:
            break
        if stop_y is not None and p[1] >= stop_y:
            break
        if p[0] > path[0][0] + 2 * radius:
            break
    return np.array(path)


def _arc(path):
    seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(seg)])


# ---------- public entry ----------


def resolve(result, spec, bgr, *, lab=None, config=None):
    """Append dense-region marker proposals to ``result["series"]``; return an audit.

    ``result`` is the extraction (series with ``points`` carrying ``px``), ``spec``
    the chart specification, ``bgr`` the panel image.  Added points carry
    ``source="dense_ribbon"`` with ``evidence_kind`` ``partial_marker`` (measured
    centre with shape support) or ``estimated`` (count-based placement).
    """
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})
    lab = lab_image(bgr) if lab is None else lab
    height, width = lab.shape[:2]
    calibration = result.get("calibration") or {}
    to_value, to_pixel = _axis_maps(calibration)
    frame = [float(v) for v in (calibration.get("frame_px") or spec.get("panel_bbox") or [0, 0, width, height])]
    if to_value("x", 0) is None or to_value("y", 0) is None:
        return {"enabled": False, "reason": "no axis models"}
    x_ticks = [float(v) for v in (spec.get("x") or {}).get("ticks") or []]
    x_min_px, x_max_px = frame[0], frame[2]
    if x_ticks:
        px_lo, px_hi = to_pixel("x", min(x_ticks)), to_pixel("x", max(x_ticks))
        if px_lo is not None and px_hi is not None:
            x_min_px, x_max_px = min(px_lo, px_hi), max(px_lo, px_hi)
    dense_x_px = x_min_px + cfg["dense_ribbon_limit_fraction"] * (x_max_px - x_min_px)
    setpoint_px = []
    for value in (spec.get("x") or {}).get("x_setpoints_visible") or []:
        try:
            pixel = to_pixel("x", float(value))
        except (TypeError, ValueError):
            continue
        if pixel is not None and math.isfinite(pixel):
            setpoint_px.append(pixel)
    stop_y = None
    if ((calibration.get("axis_models") or {}).get("y") or {}).get("scale", "linear") == "linear":
        zero = to_pixel("y", 0.0)
        if zero is not None and frame[1] < zero <= frame[3]:
            stop_y = zero - 1
    legend = spec.get("legend_bbox")
    audit = {"enabled": True, "dense_limit_px": round(float(dense_x_px), 1), "series": {}}

    for series in result.get("series", []):
        label = str(series.get("label"))
        if not series.get("extract", True):
            continue
        points = series.get("points") or []
        pts = np.array([[float(p["px"][0]), float(p["px"][1])] for p in points
                        if p.get("px") and p["px"][0] is not None and p["px"][1] is not None])
        entry = {"status": "skipped"}
        audit["series"][label] = entry
        clean = [p for p in pts if p[0] > dense_x_px] if len(pts) else []
        if len(clean) < cfg["dense_ribbon_min_clean_markers"]:
            entry["reason"] = "too few clean markers right of the dense limit"
            continue
        patches = [lab[max(0, int(p[1]) - 1):int(p[1]) + 2, max(0, int(p[0]) - 1):int(p[0]) + 2].reshape(-1, 3)
                   for p in clean]
        colour = np.median(np.concatenate(patches), axis=0).astype(np.float32)
        mask = on_ink_mask(lab, colour, cfg["dense_ribbon_color_tolerance"])
        if isinstance(legend, (list, tuple)) and len(legend) == 4:
            lx0, ly0, lx1, ly1 = [max(0, int(v)) for v in legend]
            mask[ly0:ly1, lx0:lx1] = False
        radii = []
        for p in clean:
            above = np.where(_radial_profile(mask, p[0], p[1]) > 0.5)[0]
            if len(above):
                radii.append(float(above.max() + 1))
        if not radii:
            entry["reason"] = "clean markers carry no measurable ink"
            continue
        radius = float(np.median(radii))
        marker_area = float(np.median([disc_count(mask, p[0], p[1], radius * 1.1) for p in clean]))
        half = int(math.ceil(1.3 * radius))
        template = np.median(np.stack([_crop(mask, p[0], p[1], half) for p in clean]), axis=0)
        clean_sorted = sorted(clean, key=lambda p: p[0])
        mids = [disc_count(mask, *((a + b) / 2), radius * 1.1)
                for a, b in zip(clean_sorted, clean_sorted[1:]) if np.linalg.norm(b - a) > 3 * radius]
        line_disc = float(np.median(mids)) if mids else 0.0

        on_ink = np.array([point_on_ink(mask, p, radius, marker_area, cfg["on_ink_area_fraction"]) for p in pts], bool)
        start = min(clean, key=lambda p: p[0])
        anchors = sorted([p for p, ok in zip(pts, on_ink) if ok and p[0] < start[0] - radius], key=lambda p: -p[0])
        pieces, cur, heading = [], start, np.array([-1.0, 0.0])
        for _ in range(len(anchors) + 2):
            piece = _track(mask, cur, heading, radius, frame, stop_y)
            end = cur
            if len(piece) > 1:
                pieces.append(piece)
                end = piece[-1]
                if len(piece) > 3:
                    heading = end - piece[-4]
                    heading /= max(np.linalg.norm(heading), 1e-6)
            ahead = [a for a in anchors if a[0] < min(end[0], cur[0]) - radius]
            if not ahead or (stop_y is not None and end[1] >= stop_y) or end[0] <= frame[0] + 1:
                break
            nxt = None
            for cand in ahead:
                vec = cand - end
                dist = np.linalg.norm(vec)
                if dist <= 15 * radius and float((vec / max(dist, 1e-6)) @ heading) >= math.cos(math.radians(70)):
                    nxt = cand
                    break
            if nxt is None:
                break
            if len(piece) > 1:
                pieces.append(np.array([end, nxt]))
            cur = nxt
        path = np.concatenate(pieces) if pieces else np.zeros((0, 2))
        if len(path) < 5:
            entry.update(reason=f"ribbon tracking stopped after {len(path)} points", radius_px=radius)
            continue
        arc = _arc(path)
        ink = np.array([disc_count(mask, p[0], p[1], radius * 1.15) for p in path], dtype=float)
        shape = np.array([_ncc(_crop(mask, p[0], p[1], half), template) for p in path], dtype=float)
        ink_norm = np.clip((ink - line_disc) / max(marker_area - line_disc, 1.0), 0.0, 1.2)
        score = 0.5 * np.convolve(ink_norm, np.ones(3) / 3, mode="same") + 0.5 * np.clip(shape, 0.0, 1.0)
        peaks = []
        for i in range(1, len(path) - 1):
            if score[i] >= cfg["dense_ribbon_peak_score"] and score[i] >= score[i - 1] and score[i] >= score[i + 1]:
                if peaks and arc[i] - arc[peaks[-1]] < 1.3 * radius:
                    if score[i] > score[peaks[-1]]:
                        peaks[-1] = i
                    continue
                peaks.append(i)
        step_len = float(np.median(np.diff(arc))) if len(arc) > 1 else 1.0
        discs_per_marker = (2 * radius * 1.15) / max(step_len, 1e-6)
        mass_count = float(np.clip(ink - line_disc, 0, None).sum() / (marker_area * discs_per_marker))
        region_x_max = float(path[:, 0].max())
        in_region = [p for p, ok in zip(pts, on_ink) if p[0] <= region_x_max + radius and ok]
        off_region = int(sum(1 for p, ok in zip(pts, on_ink) if p[0] <= region_x_max + radius and not ok))

        def near_existing(q, extra):
            return any(np.linalg.norm(q - e) < 0.8 * radius for e in list(in_region) + extra)

        added, new_pts = [], []
        for i in peaks:
            q = path[i]
            if near_existing(q, new_pts):
                continue
            new_pts.append(q)
            shaped = shape[i] >= cfg["dense_ribbon_shape_score"]
            added.append({
                "x": round(float(to_value("x", q[0])), 4), "y": round(float(to_value("y", q[1])), 4),
                "px": [round(float(q[0]), 2), round(float(q[1]), 2)],
                "confidence": 0.65 if shaped else 0.45, "overlap_flag": True,
                "assigned_by": "dense_ribbon", "source": "dense_ribbon",
                "evidence_kind": "partial_marker" if shaped else "estimated",
                "evidence_ref": "panel.png",
                "source_evidence": (f"Ribbon-tracked {label} ink peak at ({q[0]:.1f}, {q[1]:.1f}) px; "
                                    f"template correlation {shape[i]:.2f}, ink {ink[i]:.0f}/{marker_area:.0f}"),
                "uncertainty_px": round(0.5 * radius if shaped else radius, 1),
            })
        missing = int(round(mass_count)) - (len(in_region) + len(new_pts))
        est_pts = []
        if missing > 0:
            cands = []
            for sx in setpoint_px:
                if sx < path[:, 0].min() - radius or sx > region_x_max:
                    continue
                i = int(np.argmin(np.abs(path[:, 0] - sx)))
                q = path[i]
                if abs(q[0] - sx) <= 1.5 * radius and ink[i] >= 0.5 * marker_area and not near_existing(q, new_pts + est_pts):
                    cands.append((ink[i], q, "setpoint column"))
            cands.sort(key=lambda t: -t[0])
            placements = [(q, how) for _, q, how in cands[:missing]]
            still = missing - len(placements)
            if still > 0:
                dense_idx = [i for i in range(len(path))
                             if ink_norm[i] >= 0.5 and not near_existing(path[i], new_pts + [q for q, _ in placements])]
                if dense_idx:
                    for k in np.linspace(0, len(dense_idx) - 1, still + 2)[1:-1]:
                        q = path[dense_idx[int(round(k))]]
                        if not near_existing(q, new_pts + [c for c, _ in placements]):
                            placements.append((q, "ink mass, even spacing"))
            for q, how in placements:
                est_pts.append(q)
                added.append({
                    "x": round(float(to_value("x", q[0])), 4), "y": round(float(to_value("y", q[1])), 4),
                    "px": [round(float(q[0]), 2), round(float(q[1]), 2)],
                    "confidence": 0.4, "overlap_flag": True,
                    "assigned_by": "dense_ribbon", "source": "dense_ribbon",
                    "evidence_kind": "estimated", "evidence_ref": "panel.png",
                    "source_evidence": (f"Fused {label} band: ink mass supports about {mass_count:.0f} markers on the "
                                        f"tracked ribbon; centre placed by {how} at ({q[0]:.1f}, {q[1]:.1f}) px"),
                    "uncertainty_px": round(1.5 * radius, 1),
                })
        points.extend(added)
        series["points"] = points
        series["n_points"] = len(points)
        entry.update({
            "status": "resolved", "radius_px": round(radius, 1), "marker_area_px": int(marker_area),
            "line_disc_px": int(line_disc), "tracked_px": int(arc[-1]),
            "tracked_to_x_value": round(float(to_value("x", path[-1][0])), 3),
            "existing_on_ink_in_region": len(in_region), "existing_off_ink_in_region": off_region,
            "ink_mass_count": round(mass_count, 1), "peaks": len(peaks),
            "added_measured": len(new_pts), "added_estimated": len(est_pts),
            "path_px": [[round(float(v), 1) for v in p] for p in path[::max(1, len(path) // 60)]],
        })
    audit["added_total"] = int(sum(e.get("added_measured", 0) + e.get("added_estimated", 0)
                                   for e in audit["series"].values()))
    return audit


def prune_misassigned(result, spec, bgr, *, lab=None, config=None):
    """Drop Python-stage rows that sit on another eligible series' colour, not their own.

    Only a clear mis-assignment is removed: no ink of the row's own series in a
    1.1 r disc (against the series' own confident-marker reference) AND enough ink
    of a different eligible series there.  Rows that are merely faint are kept and
    left to the ``on_series_ink=false`` flag.  Every drop is returned in the audit.
    """
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})
    lab = lab_image(bgr) if lab is None else lab
    tolerance = cfg["on_ink_tolerance_lab"]
    fraction = cfg["on_ink_area_fraction"]
    eligible = [s for s in result.get("series", []) if s.get("extract", True) and s.get("color_lab") is not None]
    masks, references, radii = {}, {}, {}
    for series in eligible:
        label = str(series.get("label"))
        masks[label] = on_ink_mask(lab, series["color_lab"], tolerance)
        radii[label] = float(series.get("marker_radius_px") or 8.0)
        references[label] = marker_ink_reference(masks[label], series.get("points") or [], radii[label])
    dropped = {}
    for series in eligible:
        label = str(series.get("label"))
        if references[label] <= 0:
            continue
        kept = []
        for point in series.get("points") or []:
            px = point.get("px") or [None, None]
            if px[0] is None or px[1] is None or point.get("source") == "dense_ribbon":
                kept.append(point)
                continue
            if point_on_ink(masks[label], px, radii[label], references[label], fraction):
                kept.append(point)
                continue
            owner = None
            for other in eligible:
                other_label = str(other.get("label"))
                if other_label == label or references[other_label] <= 0:
                    continue
                if point_on_ink(masks[other_label], px, radii[other_label], references[other_label], fraction):
                    owner = other_label
                    break
            if owner is None:
                kept.append(point)
                continue
            dropped.setdefault(label, []).append({
                "px": [round(float(px[0]), 2), round(float(px[1]), 2)],
                "x": point.get("x"), "y": point.get("y"), "on_ink_of": owner,
            })
        series["points"] = kept
        series["n_points"] = len(kept)
    return {"enabled": True, "dropped_total": sum(len(v) for v in dropped.values()), "dropped": dropped}
