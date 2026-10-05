"""Agent 01 - read chart (agent, one call per panel).

Input: the current panel.  Python preserves the source figure, crops the panel (panel.png),
and the agent describes axes, ticks, legend and series (schemas/agent01_read_chart.schema.json).
Output: agent01/answer.json and spec.json - the extractor spec built from the answer.
A panel without any policy-eligible CO2 series stops here for human review.
"""

from __future__ import annotations

import math
import pathlib
import unicodedata

from src.models import layout, naming
from src.tools import evidence, legend_markers
from src.workflow.resources import load_json
from src.workflow.runtime_inputs import describe_runtime_inputs, resolve_runtime_inputs
from src.workflow.state import REVIEW, DocumentRun, Panel
from src.workflow.steps.base import PanelStep, read_json, save_json

CO2_SCOPE = "schemas/co2_scope.policy.json"


def valid_bbox_norm(value):
    """Return a numeric normalized bbox, or None for malformed agent output."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in box):
        return None
    x0, y0, x1, y1 = box
    return box if x1 > x0 and y1 > y0 else None


def norm_to_px(bbox_norm, width, height, pad=0.0):
    """[x0, y0, x1, y1] fractions of the image (optionally padded) -> integer pixel box."""
    x0, y0, x1, y1 = bbox_norm
    return [
        max(0, int((x0 - pad) * width)),
        max(0, int((y0 - pad) * height)),
        min(int(width), int(round((x1 + pad) * width))),
        min(int(height), int(round((y1 + pad) * height))),
    ]


def full_image_bbox_to_panel_bbox(bbox_norm, panel_bbox_norm):
    """Convert a full-image normalized box into panel-local normalized coordinates."""
    x0, y0, x1, y1 = (float(value) for value in bbox_norm)
    px0, py0, px1, py1 = (float(value) for value in panel_bbox_norm)
    pw, ph = px1 - px0, py1 - py0
    if pw <= 0 or ph <= 0:
        raise ValueError("panel bbox must have positive width and height")
    return [
        max(0.0, min(1.0, (x0 - px0) / pw)),
        max(0.0, min(1.0, (y0 - py0) / ph)),
        max(0.0, min(1.0, (x1 - px0) / pw)),
        max(0.0, min(1.0, (y1 - py0) / ph)),
    ]


def is_co2(gas) -> bool:
    text = unicodedata.normalize("NFKC", str(gas or ""))
    return "".join(character for character in text.upper() if character.isalnum()) == "CO2"


def discovery_context(panel: Panel) -> dict:
    """What agent 01 learns from agent 00: routing and document context, not series decisions."""
    return {
        "page_number": panel.figure.get("page"),
        "figure_number": panel.figure.get("figure_number"),
        "matched_caption": panel.figure.get("caption") or "",
        "physical_panel": panel.panel.get("panel", "x"),
        "source_bbox_norm": panel.panel.get("bbox_norm"),
        "routing_reason": panel.panel.get("reason", ""),
        "material_and_measurement_context": panel.discovery.get("context") or {},
    }


def panel_vector_marks(figure: dict, crop_evidence: dict, panel_dir: pathlib.Path) -> str | None:
    """Exact PDF marker paths of a vector figure, moved into panel.png pixels (None for raster figures)."""
    marks = legend_markers.load_panel_marks(figure.get("vector_marks"))
    if not marks:
        return None
    x0, y0, x1, y1 = crop_evidence["bbox_px"]
    local = []
    for mark in marks:
        bx0, by0, bx1, by1 = mark["bbox_px"]
        if bx0 >= x0 and by0 >= y0 and bx1 <= x1 and by1 <= y1:
            local.append({**mark, "bbox_px": [bx0 - x0, by0 - y0, bx1 - x0, by1 - y0],
                          "points_px": [[px - x0, py - y0] for px, py in mark["points_px"]]})
    path = layout.step_file(panel_dir, "agent01", "vector_marks.json")
    save_json(path, local)
    return str(path)


def _legend_crop_is_clipped(answer: dict) -> bool:
    """Flag a legend that the chart reader says is clipped or places at the crop edge."""
    legend = valid_bbox_norm(answer.get("legend_bbox_norm"))
    position = str(answer.get("legend_position") or "").casefold()
    return "clip" in position or bool(legend and legend[2] >= 0.995)


def _expanded_panel_bbox_for_legend(panel: Panel) -> list[float] | None:
    """Expand rightward into unused source space without crossing a sibling panel."""
    bbox = valid_bbox_norm(panel.panel.get("bbox_norm"))
    if bbox is None or bbox[2] >= 0.995:
        return None
    safe_right = 1.0
    for sibling in panel.discovery.get("panels", []):
        if str(sibling.get("panel") or "").casefold() == str(panel.panel.get("panel") or "").casefold():
            continue
        sibling_bbox = valid_bbox_norm(sibling.get("bbox_norm"))
        if sibling_bbox is None:
            continue
        vertical_overlap = min(bbox[3], sibling_bbox[3]) - max(bbox[1], sibling_bbox[1])
        if vertical_overlap <= 0.01:
            continue
        if sibling_bbox[0] >= bbox[2] - 0.005:
            safe_right = min(safe_right, sibling_bbox[0] - 0.005)
        elif sibling_bbox[2] > bbox[2] - 0.005:
            # An overlapping neighbor makes automatic expansion ambiguous.
            return None
    if safe_right <= bbox[2] + 0.01:
        return None
    return [bbox[0], bbox[1], safe_right, bbox[3]]


def build_spec(answer: dict, panel: Panel, panel_dir: pathlib.Path, width: int, height: int) -> dict:
    """Build the deterministic extractor spec from the agent 01 answer."""
    series_list = []
    for series in answer["series"]:
        anchors = series.get("y_anchors")
        series_list.append(
            {
                "label": naming.series_label(series["label"]),
                "extract": bool(series.get("extract", False)) and is_co2(series.get("gas", "CO2")),
                "gas": series.get("gas", ""),
                "species_evidence": series.get("species_evidence", ""),
                "marker": series.get("marker", "none"),
                "marker_fill": series.get("marker_fill", "filled"),
                "line_style": series.get("line_style", "solid" if series.get("has_line") else "none"),
                "color_hex": series.get("color_hex", ""),
                "quantity_type": series.get("quantity_type", answer.get("y", {}).get("title", "")),
                "loading_basis": series.get("loading_basis", "unknown"),
                "y_at_xmax": (anchors or {}).get("p100", series.get("y_at_xmax")),
                "y_anchors": anchors,
                "n_markers_estimate": series.get("n_markers_estimate"),
            }
        )
    if len(answer["x"].get("x_setpoints_visible") or []) >= 5:
        answer["x"]["grid"] = answer["x"]["x_setpoints_visible"]

    legend_norm = valid_bbox_norm(answer.get("legend_bbox_norm"))
    legend_box = norm_to_px(legend_norm, width, height, pad=0.01) if legend_norm else [0, 0, 1, 1]
    mask_boxes = []
    if panel.panel.get("has_inset") and panel.panel.get("inset_bbox_norm"):
        # Agent 00 reports panel and inset boxes in full-source-image coordinates;
        # the extractor needs boxes local to panel.png.
        inset_local = full_image_bbox_to_panel_bbox(panel.panel["inset_bbox_norm"], panel.panel["bbox_norm"])
        mask_boxes.append(norm_to_px(inset_local, width, height))

    return {
        "image": str(panel_dir / "panel.png"),
        "panel_bbox": [0, 0, width, height],
        "x": answer["x"],
        "y": answer["y"],
        "legend_bbox": legend_box,
        "legend_position": answer.get("legend_position", ""),
        "series": series_list,
        "mask_bboxes": mask_boxes,
        "context": panel.discovery.get("context"),
        "panel_info": panel.panel,
        "panel_mapping": {
            "runtime_panel_id": panel.panel_id,
            "layout_panel_id": str(panel.panel.get("panel", "") or ""),
            "canonical_panel_id": panel.panel_id,
        },
        "llm_spec": answer,
    }


class ReadChart(PanelStep):
    async def run_panel(self, run: DocumentRun, panel: Panel) -> str:
        panel_dir = run.panel_dir
        source = panel_dir / "evidence" / "manifest.json"
        if source.exists():
            source_evidence = read_json(source)
        else:
            source_evidence = evidence.preserve_source(panel.figure["file"], panel_dir / "evidence")
            save_json(source, source_evidence)
        panel_image, panel_evidence = evidence.crop_panel(
            source_evidence["preserved_path"], panel.panel["bbox_norm"], panel_dir / "panel.png", panel.panel_id, pad=0.015,
        )
        save_json(layout.step_file(panel_dir, "agent01", "evidence.json"), {**source_evidence, **panel_evidence})
        height, width = panel_image.shape[:2]

        runtime_inputs = resolve_runtime_inputs("agent01", panel_dir)
        agent_values = {"discovery_context": discovery_context(panel), "co2_scope": load_json(CO2_SCOPE),
                        "runtime_files": describe_runtime_inputs(runtime_inputs)}
        answer = await self.agent.ask(
            agent_values,
            images=[item.path for item in runtime_inputs],
            save_to=layout.step_file(panel_dir, "agent01", "answer.json"),
        )
        if _legend_crop_is_clipped(answer):
            original_bbox = list(panel.panel.get("bbox_norm") or [])
            expanded_bbox = _expanded_panel_bbox_for_legend(panel)
            if expanded_bbox is None:
                panel.status = REVIEW
                panel.message = (
                    "Agent 01 found a clipped legend, and no safe crop expansion is available. "
                    "Expand the panel crop to include the complete legend, then rerun Agent 01."
                )
                save_json(panel_dir / "qa.json", {"verdict": "review", "error": panel.message,
                                                   "legend_crop_bbox_norm": original_bbox})
                return "continue"

            save_json(layout.step_file(panel_dir, "agent01", "answer_before_legend_crop_repair.json"), answer)
            panel.panel["bbox_norm"] = expanded_bbox
            panel.panel["bbox_repair"] = {
                "reason": "Agent 01 reported a clipped legend or placed its legend box at the crop edge.",
                "original_bbox_norm": original_bbox,
                "expanded_bbox_norm": expanded_bbox,
            }
            panel_image, panel_evidence = evidence.crop_panel(
                source_evidence["preserved_path"], expanded_bbox,
                panel_dir / "panel.png", panel.panel_id, pad=0.015,
            )
            save_json(layout.step_file(panel_dir, "agent01", "evidence.json"),
                      {**source_evidence, **panel_evidence,
                       "bbox_repair": panel.panel["bbox_repair"]})
            height, width = panel_image.shape[:2]
            agent_values["discovery_context"] = discovery_context(panel)
            runtime_inputs = resolve_runtime_inputs("agent01", panel_dir)
            agent_values["runtime_files"] = describe_runtime_inputs(runtime_inputs)
            answer = await self.agent.ask(
                agent_values,
                images=[item.path for item in runtime_inputs],
                save_to=layout.step_file(panel_dir, "agent01", "answer.json"),
            )
            if _legend_crop_is_clipped(answer):
                panel.status = REVIEW
                panel.message = (
                    "Agent 01 still reports a clipped legend after one safe crop expansion. "
                    "Review the source crop and legend before running extraction."
                )
                save_json(panel_dir / "qa.json", {"verdict": "review", "error": panel.message,
                                                   "legend_crop_bbox_norm": expanded_bbox,
                                                   "legend_bbox_norm": answer.get("legend_bbox_norm")})
                return "continue"
        spec = build_spec(answer, panel, panel_dir, width, height)
        spec["vector_marks"] = panel_vector_marks(panel.figure, panel_evidence, panel_dir)
        save_json(panel_dir / "spec.json", spec)
        if not any(series["extract"] for series in spec["series"]):
            panel.status = REVIEW
            panel.message = "Agent 00 routed this panel as CO2, but agent 01 found no policy-eligible CO2 series."
            save_json(panel_dir / "qa.json", {"verdict": "review", "error": panel.message})
