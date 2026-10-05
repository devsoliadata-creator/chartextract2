"""Prototype dense-region resolver (standalone, not wired into the workflow yet).

Ribbon tracking per series + template ink-mass counting + setpoint snapping, run against one
saved panel.  Usage:

    python scripts/prototypes/dense_ribbon_resolver.py <panel_dir> [dense_limit_x_value] [out_dir]

Reads <panel_dir>/panel.png, spec.json, agent03/points.{csv,json}; writes proposals.csv, report.json,
overlay_dense_3x.png and per-series debug masks to out_dir.  See docs/dense-regions-2026-10-05.md.
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import sys
from collections import Counter, defaultdict

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from src.tools.extract import color_from_hex, color_mask, to_lab  # noqa: E402

PANEL = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                     "/home/user/chart_extract_v5.1/artifacts/runs/combined_05/fig2a")
OUT = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else pathlib.Path(__file__).resolve().parent / "dense_proto_out"
OUT.mkdir(exist_ok=True)
DENSE_LIMIT_VALUE = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0   # x value (data units) below which we search
CLEAN_MIN_VALUE = DENSE_LIMIT_VALUE                                          # markers right of this build templates

img = cv2.imread(str(PANEL / "panel.png"))
H, W = img.shape[:2]
lab = to_lab(img)
spec = json.load(open(PANEL / "spec.json", encoding="utf-8"))
cal = json.load(open(PANEL / "agent03" / "points.json", encoding="utf-8"))["calibration"]
rows = list(csv.DictReader(open(PANEL / "agent03" / "points.csv", encoding="utf-8-sig")))
frame = [float(v) for v in cal["frame_px"]]
mx, my = cal["axis_models"]["x"], cal["axis_models"]["y"]


def x_to_px(v):
    return (v - mx["intercept"]) / mx["slope"]


def y_to_px(v):
    return (v - my["intercept"]) / my["slope"]


def px_to_x(p):
    return mx["slope"] * p + mx["intercept"]


def px_to_y(p):
    return my["slope"] * p + my["intercept"]


setpoints = [float(v) for v in (spec["x"].get("x_setpoints_visible") or [])]
setpoint_px = [x_to_px(v) for v in setpoints]
dense_x_px = x_to_px(DENSE_LIMIT_VALUE)
rows_by_series = defaultdict(list)
for r in rows:
    rows_by_series[r["series_name"]].append(r)

yy, xx = np.mgrid[0:H, 0:W]


def disc_count(mask, cx, cy, rad):
    x0, x1 = max(0, int(cx - rad - 1)), min(W, int(cx + rad + 2))
    y0, y1 = max(0, int(cy - rad - 1)), min(H, int(cy + rad + 2))
    if x1 <= x0 or y1 <= y0:
        return 0
    sub = mask[y0:y1, x0:x1]
    gy, gx = np.mgrid[y0:y1, x0:x1]
    return int(((gx - cx) ** 2 + (gy - cy) ** 2 <= rad * rad)[sub > 0].sum())


def radial_profile(mask, cx, cy, rmax=24):
    """Fraction of ink in each 1-px ring around (cx, cy)."""
    x0, x1 = max(0, int(cx - rmax - 1)), min(W, int(cx + rmax + 2))
    y0, y1 = max(0, int(cy - rmax - 1)), min(H, int(cy + rmax + 2))
    sub = mask[y0:y1, x0:x1] > 0
    gy, gx = np.mgrid[y0:y1, x0:x1]
    d = np.sqrt((gx - cx) ** 2 + (gy - cy) ** 2)
    prof = []
    for k in range(rmax):
        ring = (d >= k) & (d < k + 1)
        prof.append(float(sub[ring].mean()) if ring.any() else 0.0)
    return np.array(prof)


def _ahead_centroid(mask, p, d, step, half_angle, reach=1.0):
    """Centroid of ink in the arc ahead of ``p`` at distance ~``step``*``reach``."""
    r_out = step * 1.8 * reach
    x0, x1 = max(0, int(p[0] - r_out - 1)), min(W, int(p[0] + r_out + 2))
    y0, y1 = max(0, int(p[1] - r_out - 1)), min(H, int(p[1] + r_out + 2))
    sub = mask[y0:y1, x0:x1] > 0
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
    """Move ``q`` to the ink centroid of the cross-section perpendicular to the heading."""
    n = np.array([-d[1], d[0]])
    samples = np.arange(-2.0 * radius, 2.0 * radius + 0.5, 0.5)
    pts = q[None, :] + samples[:, None] * n[None, :]
    xs, ys = np.round(pts[:, 0]).astype(int), np.round(pts[:, 1]).astype(int)
    ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
    hit = np.zeros(len(samples), bool)
    hit[ok] = mask[ys[ok], xs[ok]] > 0
    if not hit.any():
        return q
    # keep the run of ink that contains (or is nearest to) the centre line
    idx = np.where(hit)[0]
    centre = len(samples) // 2
    nearest = idx[np.argmin(np.abs(idx - centre))]
    lo = hi = nearest
    while lo - 1 >= 0 and hit[lo - 1]:
        lo -= 1
    while hi + 1 < len(samples) and hit[hi + 1]:
        hi += 1
    offset = samples[(lo + hi) // 2]
    return q + offset * n


def track_ribbon(mask, start, direction, radius, max_steps=4000, stop_y=None):
    """Follow a colour ribbon from ``start`` in ``direction`` by arc-centroid stepping.

    Never turns back (ahead window at most 90 degrees), bridges short gaps by
    extrapolating up to four steps, and stops at the frame, the zero line, or
    when it revisits its own path.
    """
    step = max(2.0, 0.6 * radius)
    p = np.array(start, dtype=float)
    d = np.array(direction, dtype=float)
    d /= np.linalg.norm(d)
    path = [p.copy()]
    for _ in range(max_steps):
        q = None
        for half_angle in (60.0, 90.0):
            q = _ahead_centroid(mask, p, d, step, half_angle)
            if q is not None:
                break
        if q is None:  # bridge a gap: look further ahead along the current heading (up to ~1 marker spacing)
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
        if float(nd @ d) < -0.2:  # would reverse
            break
        d = 0.5 * d + 0.5 * nd
        d /= np.linalg.norm(d)
        if d[0] > 0.25:  # an isotherm is single-valued in x: a leftward track never heads right
            break
        q = _recenter(mask, q, d, radius)
        p = q
        if len(path) > 25 and np.min(np.linalg.norm(np.array(path[:-20]) - p, axis=1)) < step:
            break  # revisiting an earlier stretch
        path.append(p.copy())
        if p[0] <= frame[0] + 1 or p[1] >= frame[3] - 1 or p[1] <= frame[1] + 1:
            break
        if stop_y is not None and p[1] >= stop_y:
            break
        if p[0] > path[0][0] + 2 * radius:  # the ribbon only runs leftward from the start marker
            break
    return np.array(path)


def arc_lengths(path):
    seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(seg)])


report = []
proposals = []
overlay = img.copy()
palette = [(0, 0, 255), (0, 160, 0), (255, 0, 0), (0, 140, 255), (200, 0, 200), (0, 200, 200),
           (90, 90, 90), (255, 120, 0), (120, 0, 120), (0, 100, 0), (200, 100, 50)]

for s_index, series in enumerate([s for s in spec["series"] if s.get("extract")]):
    label = series["label"]
    col = palette[s_index % len(palette)]
    existing = rows_by_series.get(label, [])
    pts = np.array([[float(r["source_pixel_x"]), float(r["source_pixel_y"])] for r in existing]) if existing else np.zeros((0, 2))
    clean = [p for p, r in zip(pts, existing) if p[0] > x_to_px(CLEAN_MIN_VALUE) and r["evidence_type"] == "visible_marker"]
    if len(clean) < 3:
        report.append({"series": label, "note": "too few clean markers to build a template"})
        continue
    # 1. measured series colour from clean marker centres (3x3 patches), tolerance from spec or default
    patches = [lab[int(p[1]) - 1:int(p[1]) + 2, int(p[0]) - 1:int(p[0]) + 2].reshape(-1, 3) for p in clean]
    colour = np.median(np.concatenate(patches), axis=0).astype(np.float32)
    tol = max(34.0, float(series.get("color_tolerance") or 28.0))  # tracking needs the anti-aliased ribbon, not just marker cores
    mask = color_mask(lab, colour, tol)
    # drop the legend
    if spec.get("legend_bbox"):
        lx0, ly0, lx1, ly1 = [int(v) for v in spec["legend_bbox"]]
        mask[ly0:ly1, lx0:lx1] = 0
    # 2. marker radius and area from the radial profile of clean markers
    radii = []
    for p in clean:
        prof = radial_profile(mask, p[0], p[1])
        above = np.where(prof > 0.5)[0]
        radii.append(float(above.max() + 1) if len(above) else 0.0)
    radius = float(np.median([r for r in radii if r > 0]))
    marker_area = float(np.median([disc_count(mask, p[0], p[1], radius * 1.1) for p in clean]))
    # line width: ink in a disc at the midpoint between consecutive clean markers, converted to width
    clean_sorted = sorted(clean, key=lambda p: p[0])
    mids = []
    for a, b in zip(clean_sorted, clean_sorted[1:]):
        m = (a + b) / 2
        if np.linalg.norm(b - a) > 3 * radius:
            mids.append(disc_count(mask, m[0], m[1], radius * 1.1))
    line_disc = float(np.median(mids)) if mids else 0.0
    line_width = line_disc / max(1.0, 2 * radius * 1.1)
    # 3. track the ribbon leftward from the leftmost clean marker
    start = min(clean, key=lambda p: p[0])
    # Existing rows are only trusted as waypoints when they sit on this series' own ink.
    on_ink = np.array([disc_count(mask, p[0], p[1], 0.8 * radius) >= 0.4 * marker_area for p in pts]) if len(pts) else np.zeros(0, bool)
    off_ink_rows = [r["point_id"] for r, ok in zip(existing, on_ink) if not ok]
    anchors = sorted([p for p, ok in zip(pts, on_ink) if ok and p[0] < start[0] - radius], key=lambda p: -p[0])  # right to left
    pieces = []
    cur, heading = start, np.array([-1.0, 0.0])
    stop_y = y_to_px(0.0) - 1
    for _guard in range(len(anchors) + 2):
        piece = track_ribbon(mask, cur, heading, radius, stop_y=stop_y)
        if len(piece) > 1:
            pieces.append(piece)
            end = piece[-1]
            if len(piece) > 3:
                heading = end - piece[-4]
                heading /= max(np.linalg.norm(heading), 1e-6)
        else:
            end = cur
        # next anchor beyond where this piece ended (leftward); restart from it
        ahead = [a for a in anchors if a[0] < min(end[0], cur[0]) - radius]  # strictly further left than where we started
        if not ahead or end[1] >= stop_y or end[0] <= frame[0] + 1:
            break
        nxt = None
        for cand in ahead:  # the next waypoint must be ahead of the current heading and not far away
            v = cand - end
            dist = np.linalg.norm(v)
            if dist <= 15 * radius and float((v / max(dist, 1e-6)) @ heading) >= math.cos(math.radians(70)):
                nxt = cand
                break
        if nxt is None:
            break
        if len(piece) > 1:  # bridge the gap to the anchor as a straight segment
            pieces.append(np.array([end, nxt]))
        cur = nxt
        if np.linalg.norm(heading) == 0:
            heading = np.array([-1.0, 0.0])
    path = np.concatenate(pieces) if pieces else np.zeros((0, 2))
    if len(path) < 5:
        report.append({"series": label, "note": f"tracker stopped after {len(path)} steps", "radius": radius})
        continue
    arc = arc_lengths(path)
    # 4. ink profile along the path; peaks = marker centres
    ink = np.array([disc_count(mask, p[0], p[1], radius * 1.15) for p in path], dtype=float)
    dbg = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    cv2.polylines(dbg, [path.astype(np.int32).reshape(-1, 1, 2)], False, (0, 0, 255), 1)
    for p_ in pts:
        cv2.circle(dbg, (int(p_[0]), int(p_[1])), 3, (0, 255, 0), 1)
    dx1 = int(dense_x_px + 60)
    cv2.imwrite(str(OUT / f"debug_{label.replace(' ', '_')}.png"),
                cv2.resize(dbg[int(frame[1]):int(frame[3]), int(frame[0]):dx1], None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
    print(f"   {label}: path pts={len(path)} ink min/med/max={ink.min():.0f}/{np.median(ink):.0f}/{ink.max():.0f} line_disc={line_disc:.0f} full_disc={np.pi*(1.15*radius)**2:.0f}")
    ink_s = np.convolve(ink, np.ones(3) / 3, mode="same")
    threshold = (line_disc + marker_area) / 2.0
    peaks = []
    for i in range(1, len(path) - 1):
        if ink_s[i] >= threshold and ink_s[i] >= ink_s[i - 1] and ink_s[i] >= ink_s[i + 1]:
            if peaks and arc[i] - arc[peaks[-1]] < 1.3 * radius:
                if ink_s[i] > ink_s[peaks[-1]]:
                    peaks[-1] = i
                continue
            peaks.append(i)
    # 5. ink-mass count over the whole tracked tube
    # Each marker is covered by about (2R / step) consecutive sampling discs, so the
    # summed excess ink over the line level, divided by area x (2R / step), counts markers
    # without assuming a separate line exists inside fused bands.
    total_len = float(arc[-1])
    step_len = float(np.median(np.diff(arc))) if len(arc) > 1 else 1.0
    discs_per_marker = (2 * radius * 1.15) / max(step_len, 1e-6)
    excess = np.clip(ink - line_disc, 0, None)
    mass_count = float(excess.sum() / (marker_area * discs_per_marker))
    tube_ink = int(excess.sum())
    # existing rows inside the tracked region
    region_x_max = float(path[:, 0].max())
    in_region = [p for p, ok in zip(pts, on_ink) if p[0] <= region_x_max + radius and ok]
    off_ink_region = [p for p, ok in zip(pts, on_ink) if p[0] <= region_x_max + radius and not ok]
    # duplicates among existing rows
    dup_pairs = 0
    for i in range(len(in_region)):
        for j in range(i + 1, len(in_region)):
            if np.linalg.norm(in_region[i] - in_region[j]) < 0.8 * radius:
                dup_pairs += 1

    def near_existing(q, extra):
        return any(np.linalg.norm(q - e) < 0.8 * radius for e in list(in_region) + extra)

    new_pts = []
    for i in peaks:
        q = path[i]
        if not near_existing(q, new_pts):
            new_pts.append(q)
            proposals.append({"series_name": label, "source_pixel_x": round(float(q[0]), 2), "source_pixel_y": round(float(q[1]), 2),
                              "x": round(px_to_x(q[0]), 4), "y": round(px_to_y(q[1]), 4),
                              "evidence_type": "partially_visible_marker", "method": "ribbon_ink_peak",
                              "uncertainty_px": round(0.5 * radius, 1)})
    peak_new = len(new_pts)
    # 6. missing count after peaks and existing rows: fill at setpoints on the path, then evenly
    accounted = len(in_region) + peak_new
    missing = int(round(mass_count)) - accounted
    est_pts = []
    if missing > 0:
        # candidate positions: setpoint columns crossing the path, with real ink there
        cands = []
        for sx in setpoint_px:
            if sx < path[:, 0].min() - radius or sx > region_x_max:
                continue
            i = int(np.argmin(np.abs(path[:, 0] - sx)))
            q = path[i]
            if abs(q[0] - sx) <= 1.5 * radius and ink[i] >= 0.5 * marker_area and not near_existing(q, new_pts + est_pts):
                cands.append((ink[i], q, "setpoint_column"))
        cands.sort(key=lambda t: -t[0])
        for _, q, how in cands[:missing]:
            est_pts.append(q)
            proposals.append({"series_name": label, "source_pixel_x": round(float(q[0]), 2), "source_pixel_y": round(float(q[1]), 2),
                              "x": round(px_to_x(q[0]), 4), "y": round(px_to_y(q[1]), 4),
                              "evidence_type": "estimated_marker", "method": how, "uncertainty_px": round(radius, 1)})
        still = missing - len(est_pts)
        if still > 0:
            # evenly along the densest stretch: path points with ink above threshold not yet covered
            dense_idx = [i for i in range(len(path)) if ink_s[i] >= threshold and not near_existing(path[i], new_pts + est_pts)]
            if dense_idx:
                picks = np.linspace(0, len(dense_idx) - 1, still + 2)[1:-1]
                for k in picks:
                    q = path[dense_idx[int(round(k))]]
                    if near_existing(q, new_pts + est_pts):
                        continue
                    est_pts.append(q)
                    proposals.append({"series_name": label, "source_pixel_x": round(float(q[0]), 2), "source_pixel_y": round(float(q[1]), 2),
                                      "x": round(px_to_x(q[0]), 4), "y": round(px_to_y(q[1]), 4),
                                      "evidence_type": "estimated_marker", "method": "ink_mass_even_spacing", "uncertainty_px": round(1.5 * radius, 1)})
    report.append({"series": label, "radius_px": round(radius, 1), "marker_area": int(marker_area), "line_width": round(line_width, 1),
                   "tracked_px": int(total_len), "tracked_to_x_value": round(px_to_x(path[-1][0]), 1),
                   "existing_in_region": len(in_region), "existing_dup_pairs": dup_pairs,
                   "existing_off_ink_total": len(off_ink_rows), "existing_off_ink_region": len(off_ink_region),
                   "ink_mass_count": round(mass_count, 1), "peaks": len(peaks), "new_peaks": peak_new,
                   "new_estimates": len(est_pts), "agent01_n_estimate": series.get("n_markers_estimate"),
                   "existing_total": len(existing), "proposed_total": len(existing) + peak_new + len(est_pts)})
    # overlay
    cv2.polylines(overlay, [path.astype(np.int32).reshape(-1, 1, 2)], False, col, 1)
    for p in in_region:
        cv2.circle(overlay, (int(round(p[0])), int(round(p[1]))), int(radius), (0, 0, 0), 1)
    for p in off_ink_region:
        cv2.drawMarker(overlay, (int(round(p[0])), int(round(p[1]))), (0, 0, 0), cv2.MARKER_CROSS, int(2 * radius), 1)
    for q in new_pts:
        cv2.circle(overlay, (int(round(q[0])), int(round(q[1]))), int(radius) + 2, col, 3)
    for q in est_pts:
        cv2.drawMarker(overlay, (int(round(q[0])), int(round(q[1]))), col, cv2.MARKER_TILTED_CROSS, int(2.5 * radius), 3)

with open(OUT / "proposals.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=["series_name", "source_pixel_x", "source_pixel_y", "x", "y", "evidence_type", "method", "uncertainty_px"])
    writer.writeheader()
    writer.writerows(proposals)
json.dump(report, open(OUT / "report.json", "w"), indent=1)
x1 = int(dense_x_px + 40)
crop = overlay[int(frame[1]):int(frame[3]) + 2, int(frame[0]) - 2:x1]
big = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
cv2.putText(big, "thin ring = existing row on its ink | thin + = existing row OFF its ink | bold ring = new ink peak | bold X = new estimate",
            (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
cv2.imwrite(str(OUT / "overlay_dense_3x.png"), big)
cv2.imwrite(str(OUT / "overlay_full.png"), overlay)
print(f"{'series':10} {'r':>4} {'area':>5} {'lw':>4} {'trk':>5} {'to_x':>6} {'exist':>5} {'offInk':>6} {'dups':>4} {'mass':>6} {'peaks':>5} {'newPk':>5} {'newEst':>6} {'a01':>4} {'total':>5}")
for r in report:
    if "note" in r:
        print(f"{r['series']:10} NOTE: {r['note']}")
        continue
    print(f"{r['series']:10} {r['radius_px']:>4} {r['marker_area']:>5} {r['line_width']:>4} {r['tracked_px']:>5} {r['tracked_to_x_value']:>6} {r['existing_in_region']:>5} {str(r['existing_off_ink_region'])+'/'+str(r['existing_off_ink_total']):>6} {r['existing_dup_pairs']:>4} {r['ink_mass_count']:>6} {r['peaks']:>5} {r['new_peaks']:>5} {r['new_estimates']:>6} {str(r['agent01_n_estimate']):>4} {r['proposed_total']:>5}")
print("proposals:", Counter((p["evidence_type"], p["method"]) for p in proposals))
