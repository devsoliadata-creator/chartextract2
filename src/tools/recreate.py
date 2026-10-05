"""
Create a visual review of extracted values, optionally including explicit trace
evidence. Similarity to the source is diagnostic and does not prove measurements.

  python recreate.py extraction.json spec.json panel.png out_prefix
  -> out_prefix_recreated.png  and  out_prefix_compare.png
"""

import json
import sys

import cv2
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.tools import extract as extractor
from src.tools import trace_paths as trace_utils

MARKERS = {
    "circle": "o",
    "square": "s",
    "triangle": "^",
    "triangle_up": "^",
    "triangle_down": "v",
    "triangle-down": "v",
    "triangle-right": ">",
    "triangle-left": "<",
    "triangle_right": ">",
    "triangle_left": "<",
    "diamond": "D",
    "star": "*",
    "pentagon": "p",
    "hexagon": "h",
    "cross": "x",
    "plus": "+",
    "none": "o",
}
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#17becf"]


def lab_to_hex(lab):
    b, g, r = cv2.cvtColor(np.uint8([[[round(v) for v in lab]]]), cv2.COLOR_LAB2BGR)[
        0, 0
    ]
    return f"#{r:02x}{g:02x}{b:02x}"


def axis_label(axis, fallback):
    """Append units only when the title does not already include them."""
    title = str(axis.get("title") or fallback).strip()
    unit = str(axis.get("unit") or "").strip()
    if not unit or title == unit or title.endswith(f"({unit})"):
        return title
    return f"{title} ({unit})"


def recreate(extraction, spec, out_png, size=(8, 5.5), curve_traces=None,
             evidence_view=False):
    cal = extraction["calibration"]
    left, top, right, bottom = cal["frame_px"]
    # axis range = the plot frame, converted with the same calibration as the points
    p0 = next(
        (
            p
            for s in extraction["series"]
            for p in s["points"]
            if p.get("px") and p["px"][0] is not None
        ),
        None,
    )
    x_anchor = (p0["x"], p0["px"][0]) if p0 else None
    y_anchor = (p0["y"], p0["px"][1]) if p0 else None
    x_of = lambda px: extractor.pixel_to_axis(cal, "x", px, x_anchor)
    y_of = lambda py: extractor.pixel_to_axis(cal, "y", py, y_anchor)
    shapes = {
        s["label"]: MARKERS.get(str(s.get("marker", "circle")).lower(), "o")
        for s in spec.get("llm_spec", {}).get("series", [])
    }
    fills = {
        s["label"]: str(s.get("marker_fill", "filled")).lower()
        for s in spec.get("llm_spec", {}).get("series", [])
    }
    marker_radius = (
        float(
            np.median(
                [
                    float(series.get("marker_radius_px") or 6.0)
                    for series in extraction["series"]
                ]
            )
        )
        if extraction["series"]
        else 6.0
    )
    curve_traces = extraction.get("curve_traces", []) if curve_traces is None else curve_traces

    fig, ax = plt.subplots(figsize=size, dpi=150)
    has_identified_traces = False
    has_legacy_samples = False
    for series_index, s in enumerate(extraction["series"]):
        color = (
            lab_to_hex(s["color_lab"])
            if s.get("color_lab") else PALETTE[series_index % len(PALETTE)]
        )
        mk = shapes.get(s["label"], "o")
        open_marker = fills.get(s["label"]) == "open"
        # Render only explicitly identified segments as paths.  Historical
        # samples with no segment identity remain neutral, unconnected dots.
        own_trace_paths = trace_utils.point_trace_paths(s["points"], s["label"])
        source_trace_paths = [
            path for path in trace_utils.trace_paths(curve_traces)
            if path.get("series_name") == s["label"]
        ]
        for path in own_trace_paths + source_trace_paths:
            trace_xy = [
                trace_utils.point_xy(point, lambda axis, pixel: x_of(pixel) if axis == "x" else y_of(pixel))
                for point in path["points"]
            ]
            trace_xy = [xy for xy in trace_xy if xy is not None]
            trace_color = path.get("color_hex") or color or "#777777"
            if len(trace_xy) >= 2 and not path["legacy_unsegmented"]:
                ax.plot(
                    [xy[0] for xy in trace_xy], [xy[1] for xy in trace_xy],
                    linestyle=(0, (4, 2)), color=trace_color, lw=1.5,
                    alpha=0.95, label="_nolegend_",
                )
                has_identified_traces = True
            elif trace_xy:
                ax.plot(
                    [xy[0] for xy in trace_xy], [xy[1] for xy in trace_xy],
                    linestyle="none", marker=".", color="#666666", ms=2.5,
                    alpha=0.8, label="_nolegend_",
                )
                has_legacy_samples = True
        # marker drawn at the same size relative to the plot frame as in the original, so the two pictures compare
        measured = [p for p in s["points"] if not trace_utils.is_trace_point(p)]
        ms = float(
            np.clip(
                2 * marker_radius / max(1.0, right - left) * size[0] * 72 * 0.7,
                3.0,
                11.0,
            )
        )
        if evidence_view:
            ax.plot(
                [p["x"] for p in measured],
                [p["y"] for p in measured],
                mk,
                color=color,
                ms=min(ms, 5.5),
                mfc="none" if open_marker else color,
                mec=color if open_marker else "black",
                mew=1.0 if open_marker else 0.4,
                ls="none",
                label=s["label"],
            )
            continue
        filled = [p for p in s["points"] if p.get("source") == "template_fill"]
        good = [p for p in measured if p["confidence"] >= 0.8]
        weak = [p for p in measured if p["confidence"] < 0.8]
        ax.plot(
            [p["x"] for p in good],
            [p["y"] for p in good],
            mk,
            color=color,
            ms=ms,
            mfc="none" if open_marker else color,
            mec=color if open_marker else "black",
            mew=1.0 if open_marker else 0.4,
            ls="none",
            label=s["label"],
        )
        if weak:
            ax.plot(
                [p["x"] for p in weak],
                [p["y"] for p in weak],
                mk,
                color=color,
                ms=ms,
                mfc="none" if open_marker else color,
                mec="red",
                mew=1.0,
                ls="none",
                alpha=0.85,
            )
        if filled:  # read on the line (grid column / curve sample), not a measured marker: hollow
            ax.plot(
                [p["x"] for p in filled],
                [p["y"] for p in filled],
                mk,
                mfc="none",
                mec=color,
                ms=ms,
                mew=1.0,
                ls="none",
                alpha=0.9,
            )
    ax.set_xlim(x_of(left), x_of(right))
    ax.set_ylim(y_of(bottom), y_of(top))
    ax.set_xticks(spec["x"]["ticks"])
    ax.set_yticks(spec["y"]["ticks"])
    if spec["x"].get("scale") == "log":
        ax.set_xscale("log")
    if spec["y"].get("scale") == "log":
        ax.set_yscale("log")
    ax.set_xlabel(axis_label(spec["x"], "x"))
    ax.set_ylabel(axis_label(spec["y"], "y"))
    title = "Saved marker rows" if evidence_view else "Extracted points"
    if has_identified_traces:
        title += "  ·  dashed = identified traces"
    if has_legacy_samples:
        title += "  ·  gray dots = legacy samples (order unavailable)"
    if not evidence_view:
        title += "  ·  red edge = low confidence"
    ax.set_title(title, fontsize=9, color="#555")
    if extraction["series"]:
        if len(extraction["series"]) > 7:
            ax.legend(
                fontsize=7, ncol=1, loc="center left", bbox_to_anchor=(1.02, 0.5),
                framealpha=0.9,
            )
            fig.tight_layout(rect=(0, 0, 0.78, 1))
        else:
            ax.legend(fontsize=7, ncol=1, loc="best", framealpha=0.9)
            fig.tight_layout()
    else:
        ax.text(
            0.5,
            0.5,
            "No series passed final QA",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=14,
            color="#8a3b35",
        )
    ax.grid(alpha=0.15)
    # Keep the actual plotting rectangle aligned with the native pixel frame.
    # A fixed figure size alone lets long labels or a large legend squeeze the
    # axes, changing the apparent chart proportions in the side-by-side review.
    plot_width = max(1.0, float(right - left))
    plot_height = max(1.0, float(bottom - top))
    ax.set_box_aspect(plot_height / plot_width)
    fig.savefig(out_png, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)
    return out_png


def side_by_side(panel_png, recreated_png, out_png):
    a = cv2.imread(panel_png)
    b = cv2.imread(recreated_png)
    h = max(a.shape[0], b.shape[0])
    a = cv2.resize(a, (int(a.shape[1] * h / a.shape[0]), h))
    b = cv2.resize(b, (int(b.shape[1] * h / b.shape[0]), h))
    gap = np.full((h, 24, 3), 255, np.uint8)
    both = np.hstack([a, gap, b])
    cv2.putText(both, "ORIGINAL", (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    cv2.putText(
        both,
        "RECREATED",
        (a.shape[1] + 36, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 0),
        2,
    )
    cv2.imwrite(out_png, both)
    return out_png


def contact_sheet(panel_png, overlay_png, extracted_png, out_png):
    """Write source, Agent 03 overlay, and CSV reconstruction in one image."""
    inputs = [
        ("SOURCE", panel_png),
        ("CANDIDATE OVERLAY", overlay_png),
        ("CANDIDATE EXTRACTED", extracted_png),
    ]
    loaded = []
    for label, path in inputs:
        image = cv2.imread(str(path))
        if image is None:
            raise RuntimeError(f"contact sheet: could not open {path}")
        loaded.append((label, image))

    target_height = max(image.shape[0] for _, image in loaded)
    cards = []
    for label, image in loaded:
        width = max(1, round(image.shape[1] * target_height / image.shape[0]))
        resized = cv2.resize(
            image, (width, target_height), interpolation=cv2.INTER_AREA
        )
        card = cv2.copyMakeBorder(
            resized,
            42,
            0,
            0,
            0,
            cv2.BORDER_CONSTANT,
            value=(255, 255, 255),
        )
        cv2.putText(
            card, label, (12, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (25, 25, 25), 2
        )
        cards.append(card)
    gap = np.full((target_height + 42, 20, 3), 245, np.uint8)
    sheet = np.hstack([cards[0], gap, cards[1], gap, cards[2]])
    cv2.imwrite(str(out_png), sheet)
    return out_png


if __name__ == "__main__":
    ext = json.load(open(sys.argv[1]))
    spec = json.load(open(sys.argv[2]))
    prefix = sys.argv[4]
    recreate(ext, spec, prefix + "_recreated.png")
    side_by_side(sys.argv[3], prefix + "_recreated.png", prefix + "_compare.png")
    print(prefix + "_compare.png")
