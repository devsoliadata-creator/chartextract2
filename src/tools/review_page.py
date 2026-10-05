"""
Review page - standalone HTML: original panel as background, extracted points on top,
drag any point (Bokeh PointDrawTool), then export the adjusted JSON.

usage: python review_page.py extraction.json extraction_panel.png review.html
"""
import copy
import json, pathlib, sys
import os
from html import escape
import numpy as np
import cv2
from bokeh.plotting import figure, output_file, save
from bokeh.resources import INLINE
from bokeh.models import (ColumnDataSource, PointDrawTool, DataTable, TableColumn, CellEditor, NumberFormatter,
                          Button, CustomJS, TextAreaInput, Div, Slider, Select, Tabs, TabPanel,
                          InlineStyleSheet, LayoutDOM, BoxZoomTool, WheelZoomTool, CheckboxGroup, Span)
from bokeh.layouts import column, row
from src.tools import trace_paths as trace_utils
CONF_OK = 0.8
PROJECT = {"folders": {"reviews": "reviews"}}

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#17becf"]
STYLESHEET_PATH = pathlib.Path(__file__).resolve().parent / "review.css"
MARKER_ALIASES = {
    "triangle_up": "triangle",
    "triangle_down": "inverted_triangle",
    "triangle-up": "triangle",
    "triangle-down": "inverted_triangle",
    "triangle_left": "triangle_pin",
    "triangle_right": "triangle_pin",
    "hexagon": "hex",
    "pentagon": "circle",
    "plus": "cross",   # bokeh "cross" is +
    "cross": "x",      # our "cross" is ×
}
VALID_MARKERS = {
    "asterisk", "circle", "circle_cross", "circle_dot", "circle_x", "circle_y",
    "cross", "dash", "diamond", "diamond_cross", "diamond_dot", "dot", "hex",
    "hex_dot", "inverted_triangle", "plus", "square", "square_cross", "square_dot",
    "square_pin", "square_x", "star", "star_dot", "triangle", "triangle_dot",
    "triangle_pin", "x", "y",
}


def pixel_to_axis_value(calibration, axis, pixel):
    """Convert a source pixel using the agent04 stage's authoritative model."""
    model = calibration["axis_models"][axis]
    transformed = float(model["slope"]) * float(pixel) + float(model["intercept"])
    return 10 ** transformed if model.get("scale") == "log" else transformed

REVIEW_CSS = """
__STYLESHEET__

body { padding-bottom: 60px; }
.review-page > div[data-root-id] {
  width: min(100%, 1280px) !important;
  margin: 0 auto;
}
:host(.review-shell) {
  width: 100%;
  max-width: 1280px;
  margin: 0 auto;
  padding-bottom: 48px;
}
:host(.review-overview) {
  gap: 12px;
  align-items: stretch;
  margin: 18px 24px 6px;
}
:host(.review-workspace) {
  gap: 16px;
  align-items: flex-start;
  margin: 6px 24px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--card);
  box-shadow: var(--shadow-sm);
}
:host(.review-section) {
  width: calc(100% - 48px);
  margin: 6px 24px;
}
:host(.review-actions) {
  width: calc(100% - 48px);
  gap: 10px;
  margin: 6px 24px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--card);
  box-shadow: var(--shadow-sm);
}
:host {
  --review-primary: #08abc1;
  --review-primary-dark: #075b73;
}
.bk-clearfix {
  display: block !important;
  width: 100%;
}
.review-header {
  padding: 20px 32px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.04);
  background: var(--primary);
  color: #ffffff;
}
.review-header h1 { margin: 0; font-size: 24px; font-weight: 650; letter-spacing: 0.01em; }
.review-header p { margin: 5px 0 0; color: rgba(255, 255, 255, 0.9); }
.review-header-inner {
  display: flex;
  gap: 18px;
  align-items: center;
  justify-content: space-between;
  max-width: 1280px;
  margin: 0 auto;
}
.review-header-nav { display: flex; flex-wrap: wrap; gap: 8px; }
.review-header-nav a,
.review-header-nav span {
  padding: 7px 11px;
  border: 1px solid rgba(255, 255, 255, 0.55);
  border-radius: var(--radius-sm);
  color: #ffffff;
  font-size: 12px;
  font-weight: 650;
  text-decoration: none;
}
.review-header-nav a:hover { background: rgba(255, 255, 255, 0.14); }
.review-header-nav span { opacity: 0.5; }
.review-card {
  height: 100%;
  margin: 0;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--card);
  color: var(--muted);
  box-shadow: var(--shadow-sm);
}
.review-card h2 { margin-top: 0; }
.review-card p:last-child { margin-bottom: 0; }
.review-card a { color: var(--primary-dark); }
.review-info { border-left: 4px solid var(--primary); }
.review-summary {
  color: var(--primary-dark);
  font-weight: 600;
  cursor: pointer;
}
.review-comparison-image {
  width: 100%;
  max-width: 1400px;
  margin-top: 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
}
.review-status-badge {
  display: inline-block;
  padding: 4px 9px;
  border-radius: 999px;
  background: var(--primary-light);
  color: var(--primary-dark);
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.review-status-badge.is-good { background: var(--good-light); color: #167743; }
.review-status-badge.is-bad { background: var(--bad-light); color: #bd2029; }
.review-status-badge.is-warn { background: var(--warn-light); color: #9b6500; }
.review-info.is-good { border-left-color: var(--good); }
.review-info.is-bad { border-left-color: var(--bad); }
.review-info.is-warn { border-left-color: var(--warn); }
.review-control-panel {
  padding: 12px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  background: #f8fafb;
}
.bk-btn-primary { background-color: var(--review-primary) !important; border-color: var(--review-primary) !important; }
.bk-btn-primary:hover { background-color: var(--review-primary-dark) !important; border-color: var(--review-primary-dark) !important; }
.bk-input, .bk-select { border-color: var(--line) !important; border-radius: var(--radius-sm) !important; }
.bk-tab { color: var(--muted); }
.bk-tab.bk-active { color: var(--primary-dark); border-color: var(--primary); }
.bk-btn:focus-visible, .bk-input:focus-visible, .bk-select:focus-visible {
  outline: 2px solid var(--review-primary);
  outline-offset: 2px;
}
@media (max-width: 700px) {
  .review-header { padding: 18px 20px; }
  .review-header h1 { font-size: 21px; }
  .review-header-inner { align-items: flex-start; flex-direction: column; }
  :host(.review-overview),
  :host(.review-workspace) { margin-right: 12px; margin-left: 12px; }
}
""".replace("__STYLESHEET__", STYLESHEET_PATH.read_text(encoding="utf-8"))
REVIEW_STYLE = f'<style id="chart-review-style">\n{REVIEW_CSS}\n</style>'
SAVE_REVIEW_JS = r"""
        out.value = txt;
        const filename = panel_id + ".json";
        const originalLabel = button.label;
        const downloadReview = () => {
            const blob = new Blob([txt], {type: "application/json"});
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = filename;
            a.click();
            URL.revokeObjectURL(a.href);
        };
        const localServer = window.location.protocol === "http:" &&
            ["127.0.0.1", "localhost"].includes(window.location.hostname);
        if (!localServer) {
            downloadReview();
            button.label = "JSON downloaded; use the review server for direct save";
            setTimeout(() => { button.label = originalLabel; }, 3500);
            return;
        }
        button.disabled = true;
        button.label = "Saving and rebuilding table...";
        fetch("/api/save-review", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({filename: filename, review: JSON.parse(txt)}),
        }).then(async (response) => {
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || "Save failed");
            button.label = `Saved; rebuilt ${result.rows} table rows`;
            // The server regenerated this page (and the comparison picture) from the saved
            // review; reload so the reviewer sees the corrected picture, not the stale one.
            setTimeout(() => window.location.reload(), 900);
        }).catch((error) => {
            console.error(error);
            button.label = "Save failed; JSON downloaded instead";
            downloadReview();
        }).finally(() => {
            button.disabled = false;
            setTimeout(() => { button.label = originalLabel; }, 4000);
        });
"""


def _apply_review_styles(root):
    """Apply the theme inside each Bokeh shadow root as well as the document page."""
    stylesheet = InlineStyleSheet(css=REVIEW_CSS)
    for component in root.select({"type": LayoutDOM}):
        component.stylesheets = [*component.stylesheets, stylesheet]
    return root


def _bokeh_marker(value):
    marker = str(value or "circle").strip().lower().replace(" ", "_")
    marker = MARKER_ALIASES.get(marker, marker)
    return marker if marker in VALID_MARKERS else "circle"


def _point_evidence_status(point):
    """Return a compact readonly status for an editable experimental marker."""
    saved_status = point.get("review_evidence_status")
    if saved_status:
        return str(saved_status)
    if point.get("manual") or point.get("assigned_by") == "human" or point.get("source") == "human_review":
        return "manual"
    value = str(point.get("evidence_kind") or point.get("evidence_type") or "").strip().lower()
    return {
        "native_visible": "visible marker",
        "visible_marker": "visible marker",
        "partial_marker": "partially visible marker",
        "partially_visible_marker": "partially visible marker",
        "estimated": "estimated marker",
        "estimated_marker": "estimated marker",
        "line_sample": "line sample",
    }.get(value, "unclassified")


def _tick_residual_label(calibration, axis):
    value = calibration.get(f"tick_fit_resid_{axis}")
    if value is not None:
        return f"{float(value):.3g}"
    pixel_value = calibration.get(f"tick_fit_resid_{axis}_px")
    return f"{float(pixel_value):.3g} px" if pixel_value is not None else "unavailable"


def _series_marker_shapes(result_path):
    """Load the canonical marker for each series from staged audit metadata/spec."""
    result_path = pathlib.Path(result_path)
    step_dir = result_path.parent  # <panel>/<step>/points.json
    panel_dir = step_dir.parent
    markers = {}
    for audit_path in (step_dir / "audit.json", panel_dir / "agent03" / "audit.json",
                       panel_dir / "agent04" / "audit.json"):
        if not audit_path.exists():
            continue
        try:
            metadata = (json.loads(audit_path.read_text(encoding="utf-8")) or {}).get("metadata") or {}
        except (OSError, ValueError, TypeError):
            continue
        for series in metadata.get("series", []):
            label = str(series.get("series_name") or series.get("label") or "")
            if label and series.get("marker_shape"):
                markers.setdefault(label, _bokeh_marker(series["marker_shape"]))
    spec_path = panel_dir / "spec.json"
    if spec_path.exists():
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            spec = {}
        for series in (spec.get("llm_spec") or {}).get("series", []):
            label = str(series.get("label") or "")
            if label and (series.get("marker_shape") or series.get("marker")):
                markers.setdefault(
                    label,
                    _bokeh_marker(series.get("marker_shape") or series.get("marker")),
                )
    return markers


def _image_frame(width, height, max_width=820, max_height=680):
    """Fit the plot while preserving the source image's exact aspect ratio."""
    scale = min(max_width / max(width, 1), max_height / max(height, 1))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _aligned_reconstruction(result, height, width, curve_traces=()):
    """Render extracted rows into the unchanged source-pixel frame."""
    image = np.zeros((height, width, 4), dtype=np.uint8)
    for index, series in enumerate(result.get("series", [])):
        lab = series.get("color_lab")
        if lab:
            blue, green, red = cv2.cvtColor(
                np.uint8([[[round(value) for value in lab]]]), cv2.COLOR_LAB2BGR,
            )[0, 0]
        else:
            color = PALETTE[index % len(PALETTE)].lstrip("#")
            red, green, blue = (int(color[offset:offset + 2], 16) for offset in (0, 2, 4))
        bgra = (int(blue), int(green), int(red), 230)
        points = [
            point.get("px") for point in series.get("points", [])
            if point.get("px") and not trace_utils.is_trace_point(point)
        ]
        centres = [(round(point[0]), round(point[1])) for point in points]
        # A result may contain points from several physical axis frames (for
        # example, a main chart and an inset).  The extraction rows do not
        # currently retain enough frame membership to join them safely, so
        # render evidence points only.  Connecting their stored order can
        # create a false line across otherwise unrelated frames.
        for centre in centres:
            cv2.circle(image, centre, 4, bgra, -1, cv2.LINE_AA)
            cv2.circle(image, centre, 4, (0, 0, 0, 255), 1, cv2.LINE_AA)
        for path in trace_utils.point_trace_paths(series.get("points", []), series.get("label")):
            _draw_trace_path_pixels(image, path, bgra[:3])
    for path in trace_utils.trace_paths(curve_traces):
        _draw_trace_path_pixels(image, path, (100, 100, 100))
    return np.flipud(image).copy().view(dtype=np.uint32).reshape(height, width)


def _draw_trace_path_pixels(image, path, fallback_bgra):
    """Draw an identified segment as a dashed line or legacy rows as neutral dots."""
    color_hex = path.get("color_hex")
    if color_hex and len(str(color_hex)) == 7:
        text = str(color_hex)
        bgra = tuple(int(text[offset:offset + 2], 16) for offset in (5, 3, 1)) + (235,)
    else:
        bgra = tuple(fallback_bgra[:3]) + (235,)
    pixels = [trace_utils.point_px(point) for point in path.get("points", [])]
    pixels = [point for point in pixels if point is not None]
    if len(pixels) >= 2 and not path.get("legacy_unsegmented"):
        # Draw short dashes along each authored edge, maintaining point order.
        for start, end in zip(pixels, pixels[1:]):
            p0 = np.asarray(start, dtype=float)
            p1 = np.asarray(end, dtype=float)
            delta = p1 - p0
            length = float(np.linalg.norm(delta))
            if length <= 0:
                continue
            direction = delta / length
            for offset in np.arange(0.0, length, 6.0):
                a = p0 + direction * offset
                b = p0 + direction * min(offset + 3.5, length)
                cv2.line(image, tuple(np.rint(a).astype(int)), tuple(np.rint(b).astype(int)),
                         bgra, 2, cv2.LINE_AA)
    else:
        for point in pixels:
            cv2.circle(image, tuple(np.rint(point).astype(int)), 2,
                       (90, 90, 90, 225), -1, cv2.LINE_AA)


def _lock_plot_ratio(plot):
    box_zoom = plot.select_one(BoxZoomTool)
    if box_zoom is not None:
        box_zoom.match_aspect = True
    wheel_zoom = plot.select_one(WheelZoomTool)
    if wheel_zoom is not None:
        wheel_zoom.zoom_on_axis = False


def _scale_markers_with_zoom(plot, renderers, x_span, y_span, base_size=11):
    """Keep markers proportional to zoom while retaining a usable size range."""
    callback = CustomJS(args=dict(
        renderers=renderers,
        x_range=plot.x_range,
        y_range=plot.y_range,
        initial_x_span=abs(float(x_span)) or 1.0,
        initial_y_span=abs(float(y_span)) or 1.0,
        base_size=base_size,
    ), code="""
        const currentX = Math.max(Math.abs(x_range.end - x_range.start), Number.EPSILON);
        const currentY = Math.max(Math.abs(y_range.end - y_range.start), Number.EPSILON);
        const zoomX = initial_x_span / currentX;
        const zoomY = initial_y_span / currentY;
        const zoom = Math.sqrt(Math.max(zoomX * zoomY, Number.EPSILON));
        const size = Math.max(6, Math.min(40, base_size * Math.sqrt(zoom)));
        for (const renderer of renderers) {
            for (const glyph of [renderer.glyph, renderer.selection_glyph,
                                 renderer.nonselection_glyph, renderer.hover_glyph,
                                 renderer.muted_glyph]) {
                if (glyph != null && glyph.size !== undefined) glyph.size = size;
            }
            renderer.change.emit();
        }
    """)
    for data_range in (plot.x_range, plot.y_range):
        data_range.js_on_change("start", callback)
        data_range.js_on_change("end", callback)


def _relative_href(target, base_dir):
    return os.path.relpath(target, base_dir).replace(os.sep, "/")


def _review_label(review_path, results_root):
    try:
        panel_dir = review_path.parent.relative_to(results_root)
        return " / ".join(panel_dir.parts)
    except ValueError:
        return review_path.parent.name


def _infer_nav(out_html, title):
    out_path = pathlib.Path(out_html).resolve()
    base_dir = out_path.parent
    results_root = None
    for parent in [base_dir, *base_dir.parents]:
        if (parent / "index.html").exists():
            results_root = parent
            break
    if results_root is None:
        return None

    reviews = sorted(results_root.rglob("review.html"), key=lambda p: _review_label(p, results_root).lower())
    if not reviews:
        reviews = [out_path]

    try:
        current_index = next(i for i, path in enumerate(reviews) if path.resolve() == out_path)
    except StopIteration:
        reviews.append(out_path)
        reviews = sorted(reviews, key=lambda p: _review_label(p, results_root).lower())
        current_index = next(i for i, path in enumerate(reviews) if path.resolve() == out_path)

    prev_path = reviews[current_index - 1] if current_index > 0 else None
    next_path = reviews[current_index + 1] if current_index < len(reviews) - 1 else None
    return {
        "title": title or _review_label(out_path, results_root),
        "index_href": _relative_href(results_root / "index.html", base_dir),
        "prev_href": _relative_href(prev_path, base_dir) if prev_path else None,
        "next_href": _relative_href(next_path, base_dir) if next_path else None,
        "self_href": _relative_href(out_path, base_dir),
        "options": [(_relative_href(path, base_dir), _review_label(path, results_root)) for path in reviews],
    }


def _header_nav(nav):
    if not nav:
        return ""

    def link(href, text):
        return f'<a href="{escape(href)}">{escape(text)}</a>' if href else f'<span>{escape(text)}</span>'

    return (
        '<nav class="review-header-nav" aria-label="Review navigation">'
        f'{link(nav.get("index_href"), "Dashboard")}'
        f'{link(nav.get("prev_href"), "Previous")}'
        f'{link(nav.get("next_href"), "Next")}'
        '</nav>'
    )


def _status_kind(status):
    value = str(status or "").lower()
    if value in {"accepted", "complete", "complete_against_reviewed_evidence"}:
        return "is-good"
    if value in {"blocked", "failed", "rejected", "error"}:
        return "is-bad"
    return "is-warn"


def _saved_review_result(result, review):
    """Overlay persisted human points for display without changing agent04/points.json."""
    if not isinstance(review, dict) or review.get("reviewed") is not True:
        return result
    reviewed = {
        str(series.get("label")): series
        for series in review.get("series", [])
        if isinstance(series, dict) and str(series.get("label") or "")
    }
    if not reviewed:
        return result

    output = copy.deepcopy(result)
    calibration = output.get("calibration") or {}
    axis_models = calibration.get("axis_models") or {}

    def pixel_position(point):
        try:
            x_model = axis_models["x"]
            y_model = axis_models["y"]
            px = (float(point["x"]) - float(x_model["intercept"])) / float(x_model["slope"])
            py = (float(point["y"]) - float(y_model["intercept"])) / float(y_model["slope"])
            return [px, py]
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    existing_labels = set()
    for series in output.get("series", []):
        label = str(series.get("label") or "")
        existing_labels.add(label)
        saved_series = reviewed.get(label)
        if saved_series is None:
            continue
        originals = series.get("points", [])
        trace_rows = [
            copy.deepcopy(point) for point in originals
            if trace_utils.is_trace_point(point)
        ]
        # New review files carry these rows explicitly. Prefer the submitted
        # readonly copy when present so the marker-only edit remains portable.
        submitted_trace_rows = saved_series.get("trace_rows")
        if submitted_trace_rows:
            trace_rows = copy.deepcopy(submitted_trace_rows)
        trace_ids = {point.get("point_id") for point in trace_rows if point.get("point_id")}
        by_id = {
            point.get("point_id"): point
            for point in originals
            if point.get("point_id") and point.get("point_id") not in trace_ids
        }
        points = []
        for saved_point in saved_series.get("points", []):
            if not isinstance(saved_point, dict):
                continue
            if saved_point.get("point_id") in trace_ids:
                continue
            point = copy.deepcopy(by_id.get(saved_point.get("point_id"), {}))
            point.update({
                "x": saved_point.get("x"),
                "y": saved_point.get("y"),
                "px": pixel_position(saved_point),
                "branch": saved_point.get("branch") or "manual",
                "confidence": saved_point.get("confidence", 1.0),
                "point_id": saved_point.get("point_id"),
                "source_instance_id": saved_point.get("source_instance_id"),
                "manual": bool(saved_point.get("manually_adjusted")),
                "assigned_by": "human",
                "source": "human_review",
                "review_evidence_status": saved_point.get("evidence_status") or _point_evidence_status(point),
            })
            points.append(point)
        series["points"] = points + trace_rows
        series["n_points"] = len(points)
        series["n_line_samples"] = len(trace_rows)

    for label, saved_series in reviewed.items():
        if label in existing_labels:
            continue
        points = []
        for saved_point in saved_series.get("points", []):
            if not isinstance(saved_point, dict):
                continue
            points.append({
                "x": saved_point.get("x"),
                "y": saved_point.get("y"),
                "px": pixel_position(saved_point),
                "branch": saved_point.get("branch") or "manual",
                "confidence": saved_point.get("confidence", 1.0),
                "point_id": saved_point.get("point_id"),
                "source_instance_id": saved_point.get("source_instance_id"),
                "manual": True,
                "assigned_by": "human",
                "source": "human_review",
                "review_evidence_status": saved_point.get("evidence_status") or "manual",
            })
        output.setdefault("series", []).append({
            "label": label,
            "points": points,
            "n_points": len(points),
        })
    return output


def build(
    result_path,
    panel_path,
    out_html,
    nav=None,
    panel_id="panel",
    status=None,
    status_reason=None,
    comparison_image="extraction_compare.png",
    comparison_label="Original vs recreated (from the extracted numbers only)",
    additional_comparison_image=None,
    human_review=None,
):
    """nav (optional): {'title', 'index_href', 'prev_href', 'next_href', 'options': [(href, label), ...]} — relative links."""
    result_path = pathlib.Path(result_path)
    res = json.load(open(result_path, encoding="utf-8"))
    res = _saved_review_result(res, human_review)
    curve_traces = res.get("curve_traces") or []
    marker_shapes = _series_marker_shapes(result_path)
    cal = res["calibration"]
    img = cv2.cvtColor(cv2.imread(panel_path), cv2.COLOR_BGR2RGBA)
    H, W = img.shape[:2]
    series_list = res.get("series") or [{"label": "manual", "points": []}]
    # Pixel -> data mapping for the review canvas. Agent 04 may provide only
    # fitted axis models, without the older unit_per_px_x/y convenience fields.
    def units_per_pixel(axis):
        legacy = cal.get(f"unit_per_px_{axis}")
        if legacy is not None:
            return float(legacy)
        model = (cal.get("axis_models") or {}).get(axis) or {}
        if model.get("scale", "linear") != "linear" or "slope" not in model:
            raise ValueError(f"Review canvas requires a linear {axis} axis model")
        return abs(float(model["slope"]))

    sx, sy = units_per_pixel("x"), units_per_pixel("y")
    # Recover intercepts from an evidence point when possible. Empty results
    # still need a review page, so fall back to the fitted linear axis models.
    p0 = next(
        (p for s in res.get("series", []) for p in s.get("points", []) if p.get("px")),
        None,
    )
    if p0 is not None:
        x_at_px0 = p0["x"] - p0["px"][0] * sx
        y_at_px0 = p0["y"] + p0["px"][1] * sy      # y pixel grows downward
    else:
        try:
            x_at_px0 = float(cal["axis_models"]["x"]["intercept"])
            y_at_px0 = float(cal["axis_models"]["y"]["intercept"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "An empty review result requires calibrated x/y axis intercepts"
            ) from error
    x0, x1 = x_at_px0, x_at_px0 + W * sx
    y_top, y_bot = y_at_px0, y_at_px0 - H * sy

    rgba = np.flipud(img).copy()
    view = rgba.view(dtype=np.uint32).reshape(H, W)

    frame_width, frame_height = _image_frame(W, H)
    fig = figure(frame_width=frame_width, frame_height=frame_height,
                 x_range=(x0, x1), y_range=(y_bot, y_top),
                 match_aspect=True, aspect_scale=abs(sy / sx) if sx else 1.0,
                 tools="pan,wheel_zoom,box_zoom,reset,save", active_scroll="wheel_zoom",
                 title="Edit extracted points")
    _lock_plot_ratio(fig)
    fig.background_fill_color = "#ffffff"
    fig.border_fill_color = "#ffffff"
    fig.outline_line_color = "#d9dee5"
    fig.title.text_color = "#182231"
    fig.title.text_font_size = "14px"
    bg = fig.image_rgba(image=[view], x=x0, y=y_bot, dw=x1 - x0, dh=y_top - y_bot, global_alpha=0.9)
    aligned = fig.image_rgba(
        image=[_aligned_reconstruction(res, H, W, curve_traces)], x=x0, y=y_bot,
        dw=x1 - x0, dh=y_top - y_bot, global_alpha=0,
    )

    sources, renderers, tables = [], [], []
    readonly_trace_rows = [
        [copy.deepcopy(point) for point in series.get("points", [])
         if trace_utils.is_trace_point(point)]
        for series in series_list
    ]
    trace_legend_keys = set()
    legacy_trace_legend_added = False
    for i, s in enumerate(series_list):
        pts = [
            point for point in s["points"]
            if not trace_utils.is_trace_point(point)
        ]
        src = ColumnDataSource(dict(
            x=[p["x"] for p in pts], y=[p["y"] for p in pts],
            x0=[p["x"] for p in pts], y0=[p["y"] for p in pts],
            conf=[p["confidence"] for p in pts], branch=[p.get("branch", "") for p in pts],
            evidence=[_point_evidence_status(p) for p in pts],
            point_id=[p.get("point_id") for p in pts],
            source_instance_id=[p.get("source_instance_id") for p in pts],
            label=[s["label"]] * len(pts),
        ))
        lab_c = s.get("color_lab")
        if lab_c:
            bb, gg, rr = cv2.cvtColor(np.uint8([[[round(v) for v in lab_c]]]), cv2.COLOR_LAB2BGR)[0, 0]
            fill = f"#{rr:02x}{gg:02x}{bb:02x}"
        else:
            fill = PALETTE[i % len(PALETTE)]
        marker = _bokeh_marker(
            s.get("marker_shape") or s.get("marker") or marker_shapes.get(s["label"])
        )
        own_trace_paths = trace_utils.point_trace_paths(s["points"], s["label"])
        source_trace_paths = [
            path for path in trace_utils.trace_paths(curve_traces)
            if path.get("series_name") == s["label"]
        ]
        for path in own_trace_paths + source_trace_paths:
            trace_xy = [
                trace_utils.point_xy(
                    point, lambda axis, pixel: pixel_to_axis_value(cal, axis, pixel)
                )
                for point in path["points"]
            ]
            trace_xy = [point for point in trace_xy if point is not None]
            trace_color = path.get("color_hex") or fill
            if len(trace_xy) >= 2 and not path["legacy_unsegmented"]:
                line_kwargs = {}
                role = trace_utils.trace_role(path)
                legend_key = (role, s["label"])
                if legend_key not in trace_legend_keys:
                    line_kwargs["legend_label"] = (
                        f"{s['label']} {role} trace - line samples, not marker centers"
                    )
                fig.line(
                    [point[0] for point in trace_xy],
                    [point[1] for point in trace_xy],
                    line_color=trace_color,
                    line_width=2.5,
                    line_alpha=0.95,
                    line_dash="dashed",
                    **line_kwargs,
                )
                trace_legend_keys.add(legend_key)
            elif trace_xy:
                sample_kwargs = {}
                if not legacy_trace_legend_added:
                    sample_kwargs["legend_label"] = (
                        "legacy line samples - segment order unavailable"
                    )
                fig.scatter(
                    [point[0] for point in trace_xy], [point[1] for point in trace_xy],
                    marker="dot", size=5, fill_color="#666666", fill_alpha=0.85,
                    line_color="#666666",
                    **sample_kwargs,
                )
                legacy_trace_legend_added = True
        r = fig.scatter("x", "y", source=src, marker=marker, size=11, fill_color=fill,
                        fill_alpha=0.35, line_color="#000000", line_width=1.5, legend_label=s["label"])
        src.js_on_change("data", CustomJS(args=dict(src=src, series_label=s["label"]), code="""
            const d = src.data;
            const n = d.x.length;
            function ensure(name, fallback) {
                if (!d[name]) d[name] = [];
                while (d[name].length < n) d[name].push(fallback);
                if (d[name].length > n) d[name] = d[name].slice(0, n);
            }
            ensure("x0", undefined);
            ensure("y0", undefined);
            ensure("conf", 1.0);
            ensure("branch", "manual");
            ensure("evidence", "manual");
            ensure("point_id", null);
            ensure("source_instance_id", null);
            ensure("label", series_label);
            for (let k = 0; k < n; k++) {
                if (d.conf[k] === "manual" || d.conf[k] == null || Number.isNaN(Number(d.conf[k]))) {
                    d.conf[k] = 1.0;
                } else {
                    d.conf[k] = Number(d.conf[k]);
                }
                if (!d.branch[k] || d.branch[k] === null) d.branch[k] = "manual";
                if (d.point_id[k] === "manual") d.point_id[k] = null;
                if (d.source_instance_id[k] === "manual") d.source_instance_id[k] = null;
                d.label[k] = series_label;
            }
        """))
        sources.append(src); renderers.append(r)
        tables.append(DataTable(source=src, width=430, height=220 if len(series_list) <= 3 else 500, editable=True, columns=[
            TableColumn(field="x", title="P", formatter=NumberFormatter(format="0.0000")),
            TableColumn(field="y", title="q", formatter=NumberFormatter(format="0.0000")),
            TableColumn(field="branch", title="branch"),
            TableColumn(field="conf", title="conf", formatter=NumberFormatter(format="0.00")),
            TableColumn(field="evidence", title="evidence / status", width=145, editor=CellEditor()),
        ]))
    fig.legend.click_policy = "hide"
    # Keep evidence layers independently inspectable instead of burning status
    # labels into the plot raster.
    guides = []
    for px in cal.get("x_ticks_px", []):
        try:
            x = float(cal["axis_models"]["x"]["slope"]) * float(px) + float(cal["axis_models"]["x"]["intercept"])
            guide = Span(location=x, dimension="height", line_color="#7c8aa0", line_alpha=.35, line_dash="dashed")
            fig.add_layout(guide); guides.append(guide)
        except (KeyError, TypeError, ValueError): pass
    for px in cal.get("y_ticks_px", []):
        try:
            y = float(cal["axis_models"]["y"]["slope"]) * float(px) + float(cal["axis_models"]["y"]["intercept"])
            guide = Span(location=y, dimension="width", line_color="#7c8aa0", line_alpha=.35, line_dash="dashed")
            fig.add_layout(guide); guides.append(guide)
        except (KeyError, TypeError, ValueError): pass
    unresolved_source = ColumnDataSource(dict(x=[], y=[]))
    for slot in (res.get("unresolved_slots") or {}).get("slots", []):
        try:
            unresolved_source.data["x"].append(pixel_to_axis_value(cal, "x", slot["column_px"]))
            unresolved_source.data["y"].append(pixel_to_axis_value(cal, "y", slot["predicted_y_px"]))
        except (KeyError, TypeError, ValueError): pass
    unresolved_renderer = fig.scatter("x", "y", source=unresolved_source, marker="x", size=13, line_color="#d62728", line_width=2)
    _scale_markers_with_zoom(fig, renderers, x1 - x0, y_top - y_bot)
    draw = PointDrawTool(renderers=renderers[:1], empty_value="manual", add=True, drag=True)
    fig.add_tools(draw)
    fig.toolbar.active_tap = draw
    fig.toolbar.active_drag = draw

    alpha = Slider(start=0, end=1, value=0.9, step=0.05, title="Original figure opacity")
    alpha.js_link("value", bg.glyph, "global_alpha")
    reconstruction_alpha = Slider(
        start=0, end=1, value=0, step=0.05,
        title="Aligned reconstruction opacity",
    )
    reconstruction_alpha.js_link("value", aligned.glyph, "global_alpha")
    add_series = Select(title="Add new points to series", value="0",
                        options=[(str(i), s["label"]) for i, s in enumerate(series_list)],
                        width=350)
    add_series.js_on_change("value", CustomJS(args=dict(plot=fig, draw=draw, renderers=renderers), code="""
        const idx = Number(cb_obj.value);
        if (renderers[idx]) {
            draw.renderers = [renderers[idx]];
            plot.toolbar.active_tap = draw;
            plot.toolbar.active_drag = draw;
        }
    """))
    layers = CheckboxGroup(labels=["Accepted points", "Calibration guides", "Unresolved slots", "All series"], active=[0, 3], width=350)
    layers.js_on_change("active", CustomJS(args=dict(renderers=renderers, guides=guides, unresolved=unresolved_renderer), code="""
        const active = new Set(cb_obj.active);
        renderers.forEach(r => r.visible = active.has(3) && active.has(0));
        guides.forEach(g => g.visible = active.has(1));
        unresolved.visible = active.has(2);
    """))

    out = TextAreaInput(rows=6, width=820, title="Saved review JSON")
    export = Button(label="Save corrections as " + panel_id + ".json", button_type="primary", width=420)
    export.js_on_click(CustomJS(args=dict(sources=sources, out=out, labels=[s["label"] for s in series_list],
                                          trace_rows=readonly_trace_rows,
                                          tol=2 * sy, panel_id=panel_id, button=export), code="""
        const series = [];
        sources.forEach((src, i) => {
            const d = src.data, pts = [];
            for (let k = 0; k < d.x.length; k++) {
                const x0 = Number(d.x0[k]);
                const y0 = Number(d.y0[k]);
                const conf = Number(d.conf[k]);
                const branch = String(d.branch[k] || "");
                const moved = !Number.isFinite(x0) || !Number.isFinite(y0) || Math.abs(d.x[k]-x0) > tol || Math.abs(d.y[k]-y0) > tol;
                pts.push({x: +(+d.x[k]).toFixed(4), y: +(+d.y[k]).toFixed(4), branch: branch || "manual",
                          confidence: Number.isFinite(conf) ? conf : 1.0,
                          evidence_status: String(d.evidence[k] || "manual"),
                          point_id: d.point_id[k] || null,
                          source_instance_id: d.source_instance_id[k] || null,
                          manually_adjusted: moved});
            }
            series.push({label: labels[i], n_points: pts.length, points: pts,
                         trace_rows: trace_rows[i] || []});
        });
        const txt = JSON.stringify({panel_id: panel_id, reviewed: true, scope: "marker_points_only",
                                    saved: new Date().toISOString(), series: series}, null, 1);
    """ + SAVE_REVIEW_JS))
    info = Div(text=f'<div class="review-card review-info"><b>calibration</b>: 1 px = {sx:.4f} x-units · {sy:.4f} y-units &nbsp;|&nbsp; '
                    f"tick residual x {_tick_residual_label(cal, 'x')}, y {_tick_residual_label(cal, 'y')}"
                    "<br>Select the target series, then click empty space to add; drag a point to move it; select + Backspace to delete.</div>",
               sizing_mode="stretch_width", css_classes=["review-section"])
    if nav is None:
        nav = _infer_nav(out_html, None)
    page_title = (nav or {}).get("title") or "chartx review"
    if len(tables) > 3:
        side = Tabs(tabs=[TabPanel(child=t, title=s["label"]) for t, s in zip(tables, series_list)], width=450)
    else:
        side = column(*tables)
    compare = Div(text='<div class="review-card"><details open><summary class="review-summary">'
                       f'{escape(str(comparison_label))}</summary>'
                       f'<img class="review-comparison-image" src="{escape(str(comparison_image))}"></details></div>',
                  sizing_mode="stretch_width", css_classes=["review-section"])
    header = Div(
        text=(
            '<div class="review-header">'
            '<div class="review-header-inner">'
            '<div>'
            f'<h1>{escape(page_title)}</h1>'
            '<p>Review extracted chart points, repair series assignments, and compare the recreated chart.</p>'
            '</div>'
            f'{_header_nav(nav)}'
            '</div>'
            '</div>'
        ),
        sizing_mode="stretch_width",
    )
    comparisons = [compare]
    if additional_comparison_image:
        comparisons.append(Div(
            text='<div class="review-card"><details open><summary class="review-summary">QA-accepted final reconstruction</summary>'
                 f'<img class="review-comparison-image" src="{escape(str(additional_comparison_image))}"></details></div>',
            sizing_mode="stretch_width",
            css_classes=["review-section"],
        ))
    controls = column(alpha, reconstruction_alpha, layers, add_series, side, width=450, css_classes=["review-control-panel"])
    workspace = row(fig, controls, sizing_mode="stretch_width", css_classes=["review-workspace"])
    actions = column(export, out, sizing_mode="stretch_width", css_classes=["review-actions"])
    body = [info, workspace, actions, *comparisons]
    overview = []
    if status:
        status_kind = _status_kind(status)
        status_card = Div(
            text=(
                f'<div class="review-card review-info {status_kind}">'
                f'<span class="review-status-badge {status_kind}">{escape(str(status).replace("_", " "))}</span>'
                f'<p><b>Status detail:</b> {escape(str(status_reason or ""))}</p>'
                '</div>'
            ),
            sizing_mode="stretch_width",
        )
        overview.append(status_card)
    if nav:
        jump = Select(title="Jump to figure", value=nav.get("self_href", ""), options=nav.get("options", []), width=350)
        jump.js_on_change("value", CustomJS(code="if (cb_obj.value) window.location.href = cb_obj.value;"))
        overview.insert(0, jump)
    if overview:
        body = [row(*overview, sizing_mode="stretch_width", css_classes=["review-overview"]), *body]
    layout = _apply_review_styles(column(header, *body, sizing_mode="stretch_width", css_classes=["review-shell"]))
    output_file(out_html, title=page_title, mode="inline")
    save(layout, resources=INLINE)
    _inject_review_css(out_html)


def build_status(
    panel_path,
    out_html,
    *,
    status,
    reason,
    context,
    artifacts=(),
    series_labels=(),
    nav=None,
    panel_id="panel",
):
    """Build a non-editable Bokeh review page for skipped or incomplete panels."""
    image = cv2.imread(str(panel_path))
    if image is None:
        image = np.full((540, 960, 3), 240, dtype=np.uint8)
    rgba = cv2.cvtColor(image, cv2.COLOR_BGR2RGBA)
    height, width = rgba.shape[:2]
    view = np.flipud(rgba).copy().view(dtype=np.uint32).reshape(height, width)
    frame_width, frame_height = _image_frame(width, height)
    fig = figure(
        frame_width=frame_width,
        frame_height=frame_height,
        x_range=(0, width),
        y_range=(0, height),
        match_aspect=True,
        tools="pan,wheel_zoom,box_zoom,reset,save",
        active_scroll="wheel_zoom",
        title="Source panel",
    )
    _lock_plot_ratio(fig)
    fig.image_rgba(image=[view], x=0, y=0, dw=width, dh=height)
    fig.axis.visible = False
    fig.grid.visible = False

    labels = [str(label) for label in series_labels if str(label).strip()] or ["manual"]
    marker_shapes = _series_marker_shapes(pathlib.Path(out_html).parent / "agent03" / "points.json")
    sources, renderers, tables = [], [], []
    for index, label in enumerate(labels):
        source = ColumnDataSource(dict(
            x=[], y=[], x0=[], y0=[], conf=[], branch=[], evidence=[], point_id=[],
            source_instance_id=[], label=[],
        ))
        renderer = fig.scatter(
            "x", "y", source=source, marker=marker_shapes.get(label, "circle"), size=11,
            fill_color=PALETTE[index % len(PALETTE)], fill_alpha=0.45,
            line_color="#000000", line_width=1.5, legend_label=label,
        )
        sources.append(source)
        renderers.append(renderer)
        tables.append(DataTable(source=source, width=430, height=220, editable=True, columns=[
            TableColumn(field="x", title="x / source pixel", formatter=NumberFormatter(format="0.00")),
            TableColumn(field="y", title="y / source pixel", formatter=NumberFormatter(format="0.00")),
            TableColumn(field="branch", title="branch"),
            TableColumn(field="conf", title="conf", formatter=NumberFormatter(format="0.00")),
            TableColumn(field="evidence", title="evidence / status", width=145, editor=CellEditor()),
        ]))
    _scale_markers_with_zoom(fig, renderers, width, height)
    draw = PointDrawTool(renderers=renderers[:1], empty_value="manual", add=True, drag=True)
    fig.add_tools(draw)
    fig.toolbar.active_tap = draw
    fig.toolbar.active_drag = draw
    selector = Select(
        title="Add new points to series",
        value="0",
        options=[(str(index), label) for index, label in enumerate(labels)],
        width=350,
    )
    selector.js_on_change("value", CustomJS(args=dict(plot=fig, draw=draw, renderers=renderers), code="""
        const idx = Number(cb_obj.value);
        if (renderers[idx]) {
            draw.renderers = [renderers[idx]];
            plot.toolbar.active_tap = draw;
            plot.toolbar.active_drag = draw;
        }
    """))
    side = Tabs(
        tabs=[TabPanel(child=table, title=label) for table, label in zip(tables, labels)],
        width=450,
    ) if len(tables) > 1 else tables[0]
    saved = TextAreaInput(rows=6, width=820, title="Saved review JSON")
    export = Button(
        label="Save corrections as " + panel_id + ".json",
        button_type="primary",
        width=420,
    )
    export.js_on_click(CustomJS(args=dict(
        sources=sources, out=saved, labels=labels, panel_id=panel_id, button=export,
    ), code="""
        const series = [];
        sources.forEach((src, i) => {
            const d = src.data, pts = [];
            for (let k = 0; k < d.x.length; k++) {
                pts.push({x: Number(d.x[k]), y: Number(d.y[k]), branch: String(d.branch[k] || "manual"),
                           confidence: Number(d.conf[k] || 1), evidence_status: String(d.evidence[k] || "manual"),
                           point_id: d.point_id[k] || null,
                           source_instance_id: d.source_instance_id[k] || null, manually_adjusted: true});
            }
            series.push({label: labels[i], n_points: pts.length, points: pts, trace_rows: []});
        });
        const txt = JSON.stringify({panel_id: panel_id, reviewed: true, scope: "marker_points_only",
                                    coordinate_space: "source_pixels", series: series}, null, 1);
    """ + SAVE_REVIEW_JS))

    if nav is None:
        nav = _infer_nav(out_html, None)
    page_title = (nav or {}).get("title") or f"{panel_id} review"
    header = Div(
        text=(
            '<div class="review-header"><div class="review-header-inner"><div>'
            f'<h1>{escape(page_title)}</h1>'
            '<p>Review status and available source artifacts for this queued panel.</p>'
            f'</div>{_header_nav(nav)}</div></div>'
        ),
        sizing_mode="stretch_width",
    )
    status_kind = _status_kind(status)
    status_card = Div(
        text=(
            f'<div class="review-card review-info {status_kind}">'
            f'<span class="review-status-badge {status_kind}">{escape(str(status).replace("_", " "))}</span>'
            f'<h2>{escape(str(panel_id))}</h2>'
            f'<p>{escape(str(context))}</p>'
            f'<p><b>Status detail:</b> {escape(str(reason))}</p>'
            '</div>'
        ),
        sizing_mode="stretch_width",
    )
    links = "".join(
        f'<li><a href="{escape(str(href))}">{escape(str(label))}</a></li>'
        for href, label in artifacts
    ) or "<li>None</li>"
    artifact_card = Div(
        text=f'<div class="review-card"><h2>Available artifacts</h2><ul>{links}</ul></div>',
        sizing_mode="stretch_width",
        css_classes=["review-section"],
    )
    editor_note = Div(
        text='<div class="review-card review-info">No calibrated candidate points are available. '
             'New points use source-image pixel coordinates.</div>',
        sizing_mode="stretch_width",
        css_classes=["review-section"],
    )
    controls = column(selector, side, width=450, css_classes=["review-control-panel"])
    workspace = row(fig, controls, sizing_mode="stretch_width", css_classes=["review-workspace"])
    overview = [status_card]
    actions = column(export, saved, sizing_mode="stretch_width", css_classes=["review-actions"])
    body = [editor_note, workspace, actions, artifact_card]
    if nav:
        jump = Select(
            title="Jump to figure",
            value=nav.get("self_href", ""),
            options=nav.get("options", []),
            width=350,
        )
        jump.js_on_change(
            "value",
            CustomJS(code="if (cb_obj.value) window.location.href = cb_obj.value;"),
        )
        overview.insert(0, jump)
    body = [row(*overview, sizing_mode="stretch_width", css_classes=["review-overview"]), *body]
    layout = _apply_review_styles(column(header, *body, sizing_mode="stretch_width", css_classes=["review-shell"]))
    output_file(out_html, title=page_title, mode="inline")
    save(layout, resources=INLINE)
    _inject_review_css(out_html)


def _inject_review_css(out_html):
    path = pathlib.Path(out_html)
    html = path.read_text(encoding="utf-8")
    css = REVIEW_STYLE.strip()
    if css in html:
        return
    head_close = html.rfind("</head>")
    if head_close < 0:
        return
    html = html[:head_close] + css + "\n" + html[head_close:]
    html = html.replace("<body>", '<body class="review-page">', 1)
    path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -m src.tools.review_page <extraction.json> <panel.png> <review.html>")
    build(sys.argv[1], sys.argv[2], sys.argv[3])
