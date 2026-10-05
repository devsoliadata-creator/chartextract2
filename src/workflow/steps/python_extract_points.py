"""The Python extraction - extract points (deterministic Python, no model call). Runs every round of the check loop.

Input: spec.json - the agent 01 reading, plus agent 02's corrections from earlier rounds.
src/tools/extract.py calibrates the axes from the printed ticks (and tick anchors once agent 02
has supplied them) and detects every marker of every CO2 series.
Output: the python stage (python/points.csv/json, python/images/ with overlay, re-plot and
side-by-side images), python/audit.json (hard checks + extraction audit) and
python/summary.json.  When calibration fails - or succeeds but the values contradict the printed
axis (hard axis-range check) - the error is recorded and the detected tick candidates are drawn
(python/images/tick_check.png) so agent 02 can fix the mapping.
Always continues to agent 02, which decides whether to accept, adjust and rebuild, or give up.
"""

from __future__ import annotations

import copy
import json
import pathlib
import shutil

import cv2

from src.models import layout
from src.tools import dense_regions, evidence, extract, hard_checks, legend_markers, recreate, round_score, stage_artifacts
from src.workflow.state import CONTINUE, FAILED, REVIEW_POINTS, DocumentRun, Panel
from src.workflow.steps.base import PanelStep, read_json, save_json


def legend_marker_check(spec: dict, stage: dict) -> list[dict]:
    """Declared series style (agent 01 / agent 02) next to the glyph measured from the legend pixels."""
    declared = {str(series.get("label")): series for series in spec.get("series", [])}
    rows = []
    for glyph in (stage.get("calibration") or {}).get("legend_markers", []):
        style = declared.get(str(glyph.get("label")), {})
        measured = {key: glyph.get(key) for key in ("shape", "fill", "color_hex", "fill_hex", "edge_hex", "size_px",
                                                     "source", "confidence")}
        rows.append({
            "label": glyph.get("label"),
            "declared": {key: style.get(key) for key in ("marker", "marker_fill", "color_hex")},
            "measured": measured,
            "mismatches": legend_markers.compare(style, glyph),
        })
    return rows


AXIS_CHECK_FAILED = "Axis range check failed"


def count_check(spec: dict, stage: dict) -> list[dict]:
    """Agent 02's expected marker counts per x range next to what Python found."""
    found = {item["label"]: item for item in stage.get("series", [])}
    rows = []
    for series in spec.get("series", []):
        # Dense trace samples describe continuous ink, not experimental marker
        # instances.  Agent 02's expected counts and round scoring therefore
        # use marker candidates only.
        points = [point for point in (found.get(series.get("label")) or {}).get("points", [])
                  if point.get("source") not in {"curve", "grid"}]
        for expected in series.get("expected_counts") or []:
            n = sum(expected["x_from"] <= float(p["x"]) <= expected["x_to"] for p in points)
            rows.append({"label": series.get("label"), "x_from": expected["x_from"], "x_to": expected["x_to"],
                         "expected": expected["count"], "found": n, "shortfall": max(0, expected["count"] - n)})
    return rows


AUDIT_KEYS = ("series_assignment_audit", "series_gate", "template_fill", "complex_regions_accepted",
              "complex_regions_rejected", "legend_markers")


def _counts(items, key):
    counts = {}
    for item in items or []:
        counts[str(item.get(key))] = counts.get(str(item.get(key)), 0) + 1
    return counts


def compact_calibration(calibration: dict) -> dict:
    """The calibration an agent can reason about; per-candidate audits stay in python/audit.json."""
    assignment = calibration.get("series_assignment_audit") or {}
    template = calibration.get("template_fill") or {}
    return {
        **{key: value for key, value in calibration.items() if key not in AUDIT_KEYS},
        "series_assignment": {
            "actions": _counts(assignment.get("candidates"), "action"),
            "groups": [{"series_labels": group.get("series_labels"), "assigned_counts": group.get("assigned_counts"),
                        "unresolved_count": group.get("unresolved_count")} for group in assignment.get("groups", [])],
        },
        "series_gate_moves": [{"from": key.split(" -> ")[0], "to": key.split(" -> ")[1], "points": n} for key, n in
                              _counts([{"k": f"{m.get('from')} -> {m.get('to')}"} for m in calibration.get("series_gate") or []],
                                      "k").items()],
        "template_fill": {key: template.get(key) for key in ("template_fit", "template_fill")},
        "complex_regions": {"accepted": len(calibration.get("complex_regions_accepted") or []),
                            "rejected": len(calibration.get("complex_regions_rejected") or [])},
        "detail": "full per-candidate audit in python/audit.json",
    }


def candidate_outlier_failure(hard: dict) -> bool:
    """Allow calibrated candidate points outside the axes onward for correction.

    A failure that also shows a shifted/stretched plot frame remains an extraction
    failure. Only a clean frame-to-tick fit with outlying point values is deferred.
    """
    axes = (hard.get("axis_range") or {})
    failed = [entry for axis, entry in axes.items()
              if axis in {"x", "y"} and not entry.get("passed", True)]
    if not failed:
        return False
    return all(entry.get("points_outside", 0) > 0
               and entry.get("frame_passed") is True
               and entry.get("frame_overhang_steps") is not None
               for entry in failed)


def python_summary(spec: dict, stage: dict) -> dict:
    """What agents 02 and 03 need to know about the Python result (saved as python/summary.json).

    Kept compact on purpose: it is pasted into agent prompts.  Per-candidate audits (thousands of
    entries on a 17-series chart) go to python/audit.json instead.
    """
    calibration = stage.get("calibration") or {}
    optimization_groups = {}
    for item in spec.get("series", []):
        if not item.get("extract"):
            continue
        label = str(item.get("label") or "")
        group = str(item.get("optimization_group") or f"series:{label}")
        optimization_groups.setdefault(group, []).append(label)
    return {
        "series_counts": {
            item["label"]: sum(point.get("source") not in {"curve", "grid"}
                               for point in item.get("points", []))
            for item in stage.get("series", [])
        },
        # Supporting traces are stored separately from candidate points. Keep
        # their telemetry explicit so a trace never changes a marker count.
        "trace_sample_counts": dict((calibration.get("dense_band_sampling") or {}).get("samples_by_series") or {}),
        "trace_sample_count": int((calibration.get("dense_band_sampling") or {}).get("trace_sample_count") or 0),
        "calibration": compact_calibration(calibration),
        "proposal_quality": stage.get("proposal_quality", {}),
        "color_ambiguity_groups": [{key: group.get(key) for key in ("series_labels", "pair_distances_lab")}
                                    for group in calibration.get("color_ambiguity_groups", [])],
        "unresolved_slots": stage.get("unresolved_slots", {}),
        "complex_regions": spec.get("complex_regions", []),
        "legend_marker_check": legend_marker_check(spec, stage),
        "source_integrity": evidence.source_integrity(spec),
        "count_check": count_check(spec, stage),
        "sample_colors": calibration.get("sample_colors", []),
        "region_strategies": calibration.get("region_strategies", []),
        "optimization_groups": optimization_groups,
    }


def write_python_stage(panel_dir, extraction, spec, panel: Panel, source_pdf: str, supporting_traces=None):
    """Write the python stage files and its inspection images. Returns (stage, rows, metadata)."""
    stage, rows, metadata = stage_artifacts.write_stage(
        panel_dir, "python", extraction, spec, panel.figure_id, panel.panel_id, source_pdf=source_pdf,
    )
    dense = dense_regions.write_evidence(cv2.imread(spec["image"]), stage, spec, layout.images_dir(panel_dir, "python") / "dense")
    if dense:
        metadata["dense_region_evidence"] = dense
    images_dir = layout.images_dir(panel_dir, "python")
    extract.redraw_overlay(stage, spec, str(images_dir / "overlay.png"))
    recreate.recreate(stage, spec, str(images_dir / "recreated.png"), curve_traces=supporting_traces)
    recreate.side_by_side(str(panel_dir / "panel.png"), str(images_dir / "recreated.png"),
                          str(images_dir / "compare.png"))
    measured = (stage.get("calibration") or {}).get("legend_markers", [])
    sheet = images_dir / "legend_markers.png"
    sheet.unlink(missing_ok=True)
    if measured:
        legend_markers.legend_sheet(cv2.imread(spec["image"]), [m["legend_box_px"] for m in measured], measured,
                                    [m["label"] for m in measured], sheet)
    return stage, rows, metadata


STALE_ON_SUCCESS = ("tick_check.png", "tick_candidates.json")
PYTHON_STAGE_IMAGES = ("overlay.png", "recreated.png", "compare.png", "redraw_diff.png")
PYTHON_STAGE_FILES = ("points.json", "points.csv", "summary.json", "audit.json",
                      "supporting_traces.json", "series_round_selection.json")


def snapshot_round(panel_dir, n, total_points: int, hard: dict, summary: dict) -> dict:
    """Copy the just-written python/ stage into python/rounds/round<n>/ and score it.

    Round snapshots preserve the exact candidate and per-series settings that
    produced it.  Their per-series scores are later used to compose the final
    Python candidate before Agent 03.
    """
    panel_dir = pathlib.Path(panel_dir)
    python_dir = layout.step_dir(panel_dir, "python")
    round_dir = layout.python_round_dir(panel_dir, n)
    for existing in round_dir.iterdir():  # re-extraction on rerun: start clean
        shutil.rmtree(existing) if existing.is_dir() else existing.unlink()
    for item in python_dir.iterdir():
        if item.name == "rounds":
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, round_dir / item.name)
    shutil.copy2(panel_dir / "spec.json", round_dir / "spec.json")
    score = round_score.score_round(
        hard, summary.get("count_check"), total_points,
        series_counts=summary.get("series_counts"),
    )
    save_json(round_dir / "score.json", {"round": int(n), **score})
    return score


def round_scores(panel_dir, through_round: int | None = None) -> dict:
    """Load round scores, deriving per-series detail for older snapshots."""
    rounds_dir = layout.step_dir(panel_dir, "python") / "rounds"
    scores = {}
    for path in sorted(rounds_dir.glob("round*/score.json")):
        score = read_json(path)
        round_no = int(score["round"])
        if through_round is not None and round_no > int(through_round):
            continue
        if not score.get("per_series"):
            summary_path = path.parent / "summary.json"
            if summary_path.is_file():
                summary = read_json(summary_path)
                hard = summary.get("hard_checks") or {
                    "passed": score.get("axis_passed"),
                    "redraw": {"recall": score.get("recall"), "series": {}},
                }
                derived = round_score.score_round(
                    hard, summary.get("count_check"), score.get("total_points", 0),
                    series_counts=summary.get("series_counts"),
                )
                score = {**score, "per_series": derived["per_series"]}
        scores[round_no] = score
    return scores


def _dedupe_dicts(items):
    seen, output = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            output.append(copy.deepcopy(item))
    return output


def merge_best_series_snapshots(panel_dir, latest_round_no: int):
    """Compose extraction/spec/traces from each series' highest-scoring round.

    Global axes and calibration stay on the latest checked round.  Per-series
    points and extraction parameters come from that series' selected snapshot.
    Scoped region strategies and supporting traces follow the same winner.
    """
    panel_dir = pathlib.Path(panel_dir)
    scores = round_scores(panel_dir, through_round=latest_round_no)
    selected = round_score.pick_best_by_series(scores)
    latest_dir = layout.python_round_dir(panel_dir, latest_round_no)
    latest_extraction = read_json(latest_dir / "points.json")
    latest_spec = read_json(latest_dir / "spec.json")
    round_cache = {}

    def load_round(round_no):
        if round_no not in round_cache:
            folder = layout.python_round_dir(panel_dir, round_no)
            traces_path = folder / "supporting_traces.json"
            round_cache[round_no] = (
                read_json(folder / "points.json"),
                read_json(folder / "spec.json"),
                read_json(traces_path) if traces_path.is_file() else {"traces": []},
            )
        return round_cache[round_no]

    merged = copy.deepcopy(latest_extraction)
    merged_spec = copy.deepcopy(latest_spec)
    latest_by_label = {str(item.get("label")): item for item in latest_extraction.get("series", [])}
    merged_series = []
    actual_selection = {}
    for latest_series in latest_extraction.get("series", []):
        label = str(latest_series.get("label"))
        round_no = int(selected.get(label, latest_round_no))
        source, _, _ = load_round(round_no)
        source_series = next((item for item in source.get("series", []) if str(item.get("label")) == label), None)
        if source_series is None:
            round_no, source_series = latest_round_no, latest_by_label[label]
        merged_series.append(copy.deepcopy(source_series))
        actual_selection[label] = round_no
    merged["series"] = merged_series

    # Keep the latest global structure, but replace each series' extractor
    # settings with the settings from its winning round.
    spec_by_label = {str(item.get("label")): item for item in merged_spec.get("series", [])}
    for label, round_no in actual_selection.items():
        _, source_spec, _ = load_round(round_no)
        source_item = next((item for item in source_spec.get("series", []) if str(item.get("label")) == label), None)
        if source_item is not None:
            spec_by_label[label] = copy.deepcopy(source_item)
    merged_spec["series"] = [spec_by_label.get(str(item.get("label")), item)
                             for item in latest_spec.get("series", [])]

    # Global ignore regions stay current. Series-scoped strategies follow the
    # selected settings for the series they affect.
    strategies = [item for item in latest_spec.get("region_strategies", [])
                  if not item.get("series_labels")]
    for label, round_no in actual_selection.items():
        _, source_spec, _ = load_round(round_no)
        strategies.extend(item for item in source_spec.get("region_strategies", [])
                          if label in [str(value) for value in item.get("series_labels") or []])
    merged_spec["region_strategies"] = _dedupe_dicts(strategies)

    unresolved = []
    latest_slots = (latest_extraction.get("unresolved_slots") or {}).get("slots") or []
    unresolved.extend(copy.deepcopy(slot) for slot in latest_slots if not slot.get("series_label"))
    for label, round_no in actual_selection.items():
        source, _, _ = load_round(round_no)
        unresolved.extend(copy.deepcopy(slot) for slot in (source.get("unresolved_slots") or {}).get("slots") or []
                          if str(slot.get("series_label")) == label)
    by_series = {}
    for slot in unresolved:
        label = str(slot.get("series_label") or "")
        if label:
            by_series[label] = by_series.get(label, 0) + 1
    merged["unresolved_slots"] = {"total": len(unresolved), "by_series": by_series, "slots": unresolved}

    traces = []
    for label, round_no in actual_selection.items():
        _, _, source_traces = load_round(round_no)
        traces.extend(copy.deepcopy(trace) for trace in source_traces.get("traces", [])
                      if str(trace.get("series_name")) == label)
    merged_traces = {"traces": traces, "role": "supporting_only",
                     "series_round_selection": actual_selection}
    merged.setdefault("calibration", {})["series_round_selection"] = actual_selection
    return merged, merged_spec, merged_traces, actual_selection, scores


def compose_best_series_rounds(panel_dir, latest_round_no: int, panel: Panel, source_pdf: str):
    """Write the per-series winning composite and regenerate its review chart."""
    extraction, spec, supporting_traces, selected, scores = merge_best_series_snapshots(
        panel_dir, latest_round_no,
    )
    save_json(pathlib.Path(panel_dir) / "spec.json", spec)
    stage, rows, metadata = write_python_stage(
        panel_dir, extraction, spec, panel, source_pdf, supporting_traces=supporting_traces,
    )
    save_json(layout.step_file(panel_dir, "python", "supporting_traces.json"), supporting_traces)
    hard = hard_checks.run(stage, spec, panel_dir, "python")
    metadata.update(
        composition="per_series_best_round",
        latest_round=int(latest_round_no),
        selected_round_by_series=selected,
        round_scores=scores,
        supporting_traces={
            "path": "python/supporting_traces.json", "role": "supporting_only",
            "trace_count": len(supporting_traces.get("traces", [])),
            "trace_sample_count": sum(len(item.get("ordered_points") or [])
                                      for item in supporting_traces.get("traces", [])),
        },
    )
    stage_artifacts.write_audit(
        panel_dir, "python", metadata=metadata, hard_checks=hard,
        extraction_audit={key: (stage.get("calibration") or {}).get(key) for key in AUDIT_KEYS},
    )
    summary = {**python_summary(spec, stage), "hard_checks": hard,
               "series_round_selection": selected, "composition": "per_series_best_round"}
    save_json(layout.step_file(panel_dir, "python", "summary.json"), summary)
    save_json(layout.step_file(panel_dir, "python", "series_round_selection.json"), {
        "strategy": "per_series_best_round", "latest_round": int(latest_round_no),
        "selected_round_by_series": selected, "scores": scores,
    })
    answer_path = layout.step_file(panel_dir, "agent02", "answer.json")
    if answer_path.is_file():
        answer = read_json(answer_path)
        answer["selected_round"] = {
            "selected": int(latest_round_no), "latest": int(latest_round_no),
            "strategy": "per_series_best_round", "selected_by_series": selected,
            "agent_approved": bool(answer.get("satisfied")),
            "authority": ("agent02_source_review_per_series_composite"
                          if answer.get("satisfied") else "per_series_composite_requires_review"),
            "scores_role": "per_series_selection", "scores": scores,
            "reason": "Python rebuilt the Agent 03 input from each series' best extraction round.",
        }
        save_json(answer_path, answer)
    panel.rows["python"] = len(rows)
    return {"selected_round_by_series": selected, "scores": scores, "hard_checks": hard}


def record_error(panel_dir, panel: Panel, spec: dict, message: str) -> None:
    """Keep the error for agent 02 and draw the detected tick candidates so it can fix the mapping."""
    panel.extract_error = message
    layout.step_file(panel_dir, "python", "error.txt").write_text(message, encoding="utf-8")
    images_dir = layout.images_dir(panel_dir, "python")
    try:
        candidates = extract.render_tick_check(spec, str(images_dir / "tick_check.png"))
        save_json(images_dir / "tick_candidates.json", candidates)
    except Exception:  # no axis frame -> nothing to draw; agent 02 still sees the error
        for name in ("tick_check.png", "tick_candidates.json"):
            (images_dir / name).unlink(missing_ok=True)


class ExtractPoints(PanelStep):
    @staticmethod
    def after_final_agent02_patch(panel: Panel) -> str:
        """Route the one final Agent 02 patch to review, without asking Agent 02 again."""
        if not panel.agent02_final_patch:
            return CONTINUE
        panel.agent02_final_patch = False
        if panel.extract_error:
            panel.status = FAILED
            panel.message = "Final Agent 02 patch did not produce a reviewable Python extraction: " + panel.extract_error
            return CONTINUE
        panel.needs_review = True
        return REVIEW_POINTS

    async def run_panel(self, run: DocumentRun, panel: Panel) -> str:
        panel_dir = run.panel_dir
        spec = read_json(panel_dir / "spec.json")
        images_dir = layout.images_dir(panel_dir, "python")
        try:
            extraction, _ = extract.extract(spec)
        except RuntimeError as error:  # includes TickAnchorMismatchError and axis/tick detection failures
            for name in PYTHON_STAGE_IMAGES:  # no stale images from an earlier round
                (images_dir / name).unlink(missing_ok=True)
            for name in PYTHON_STAGE_FILES:
                layout.step_file(panel_dir, "python", name).unlink(missing_ok=True)
            record_error(panel_dir, panel, spec, str(error))
            return self.after_final_agent02_patch(panel)

        # Native continuous bands are useful review evidence, but are not
        # measurements and therefore do not enter the canonical candidate
        # CSV/JSON. The separate artifact preserves ordered segment samples.
        supporting_traces, trace_audit = dense_regions.build_supporting_traces(
            extraction, spec, panel_dir / "panel.png"
        )
        trace_audit["legacy_proposals"] = dense_regions.move_supporting_point_proposals(
            extraction, supporting_traces, spec
        )
        trace_audit["trace_count"] = len(supporting_traces["traces"])
        trace_audit["trace_sample_count"] = sum(
            len(trace["ordered_points"]) for trace in supporting_traces["traces"]
        )
        trace_audit["samples_by_series"] = {}
        for trace in supporting_traces["traces"]:
            label = str(trace.get("series_name") or "")
            trace_audit["samples_by_series"][label] = (
                trace_audit["samples_by_series"].get(label, 0) + len(trace["ordered_points"])
            )
        extraction.setdefault("calibration", {})["dense_band_sampling"] = trace_audit

        for name in STALE_ON_SUCCESS:
            (images_dir / name).unlink(missing_ok=True)
        layout.step_file(panel_dir, "python", "series_round_selection.json").unlink(missing_ok=True)
        layout.step_file(panel_dir, "python", "error.txt").unlink(missing_ok=True)
        stage, rows, metadata = write_python_stage(
            panel_dir, extraction, spec, panel, run.source, supporting_traces=supporting_traces
        )
        save_json(layout.step_file(panel_dir, "python", "supporting_traces.json"), supporting_traces)
        metadata["supporting_traces"] = {
            "path": "python/supporting_traces.json", "role": "supporting_only",
            "trace_count": trace_audit.get("trace_count", 0),
            "trace_sample_count": trace_audit.get("trace_sample_count", 0),
        }
        hard = hard_checks.run(stage, spec, panel_dir, "python")
        stage_artifacts.write_audit(panel_dir, "python", metadata=metadata, hard_checks=hard,
                                    extraction_audit={key: (stage.get("calibration") or {}).get(key) for key in AUDIT_KEYS})
        summary = {**python_summary(spec, stage), "hard_checks": hard}
        save_json(layout.step_file(panel_dir, "python", "summary.json"), summary)
        panel.rows["python"] = len(rows)
        panel.extract_error = ""
        marker_rows = sum(row["evidence_type"] != "line_sample" for row in rows)
        latest_round_no = panel.check_rounds + 1
        snapshot_round(panel_dir, latest_round_no, marker_rows, hard, summary)
        if panel.agent02_final_patch:
            # The last Agent 02 patch is not checked by another model call.
            # Select each series from all available rounds and regenerate the
            # exact composite that Agent 03 will receive.
            composite = compose_best_series_rounds(
                panel_dir, latest_round_no, panel, run.source,
            )
            hard = composite["hard_checks"]
        if not hard["passed"] and not candidate_outlier_failure(hard):
            # A shifted or stretched axis remains blocking. Point-only outliers
            # are reviewable candidates and continue to Agents 03/04.
            record_error(panel_dir, panel, spec, f"{AXIS_CHECK_FAILED}: " + "; ".join(hard["problems"]))
        return self.after_final_agent02_patch(panel)
