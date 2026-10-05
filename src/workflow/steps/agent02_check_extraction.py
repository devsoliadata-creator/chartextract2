"""Agent 02 - plan and verify the deterministic Python extraction.

Input: the source panel, the Python overlay and re-plot from the Python extraction (or, when calibration
failed, the error and the drawn tick candidates) and the structure Python used.  The agent
checks calibration, series identity and coverage (schemas/agent02_check_extraction.schema.json).
The bounded protocol has two calls: the first creates grouped per-series extraction plans and
the second verifies them. A non-satisfied second response is an executable targeted patch.
Before Agent 03, Python selects each series' best extraction round, merges those winning series
and settings, and regenerates the final comparison chart.
Output: agent02/answer.json (latest) and agent02/round<N>.json per round, each holding the
checked structure plus its "application" (what was applied / unexecuted_retry_limit) and, when
anchors were snapped to tick candidates, its "tick_reconciliation".
"""

from __future__ import annotations

import copy
import json
import math

from src.models import layout, naming
from src.settings import CFG
from src.tools import round_score
from src.workflow.resources import load_workflow
from src.workflow.runtime_inputs import resolve_runtime_inputs
from src.workflow.state import ADJUST, CONTINUE, DocumentRun, Panel
from src.workflow.steps.base import PanelStep, read_json, save_json
from src.workflow.steps.agent01_read_chart import norm_to_px, valid_bbox_norm
from src.workflow.steps.python_extract_points import AXIS_CHECK_FAILED
from src.workflow.steps.python_extract_points import (
    candidate_outlier_failure,
    compose_best_series_rounds,
    round_scores as load_round_scores,
)

STYLE_KEYS = ("label", "marker", "marker_fill", "line_style", "color_hex")


def _normalise(value):
    return "".join(naming.series_label(value).lower().split())


def match_checked_series(original, checked):
    """Pair agent 02 series with the current identities without trusting their order."""
    remaining = set(range(len(original)))
    matches = []
    for checked_index, checked_series in enumerate(checked):
        label = _normalise(checked_series.get("label"))
        label_matches = [i for i in remaining if _normalise(original[i].get("label")) == label]
        if len(label_matches) == 1:
            match = label_matches[0]
        else:

            def score(index):
                candidate = original[index]
                return sum(
                    _normalise(candidate.get(key)) == _normalise(checked_series.get(key))
                    and bool(_normalise(checked_series.get(key)))
                    for key in ("color_hex", "marker", "marker_fill", "line_style")
                )

            ranked = sorted(((score(i), i) for i in remaining), reverse=True)
            best_score = ranked[0][0] if ranked else 0
            best = [i for value, i in ranked if value == best_score]
            if best_score > 0 and len(best) == 1:
                match = best[0]
            elif checked_index in remaining:
                match = checked_index
            else:
                match = None
        if match is not None:
            remaining.remove(match)
        matches.append(match)
    return matches


def reconcile_retry_tick_anchors(check, tick_candidates):
    """Snap anchors to the exact tick candidates Python detected and the agent identified.

    This corrects image-coordinate estimation noise, not calibration disagreement: an anchor
    must stay close to one unique candidate, so a shifted tick/value mapping still fails
    the unchanged strict gates when the Python extraction extracts again.
    """
    reconciled = copy.deepcopy(check or {})
    audit = {"axes": {}}
    for axis_id, report_key, extent_key in (("x", "x_axis", "image_width_px"), ("y", "y_axis", "image_height_px")):
        anchors = list((reconciled.get(report_key) or {}).get("tick_anchors") or [])
        candidates = list((tick_candidates or {}).get(axis_id) or [])
        extent = float((tick_candidates or {}).get(extent_key) or 0)
        pixels = sorted(float(item["candidate_pixel"]) for item in candidates)
        spacings = [right - left for left, right in zip(pixels, pixels[1:]) if right > left]
        snap_limit = min(24.0, 0.2 * float(sorted(spacings)[len(spacings) // 2])) if spacings else 0.0
        used, entries = set(), []
        for anchor in anchors:
            entry = {"input": dict(anchor), "snapped": False}
            try:
                anchor_px = float(anchor["pixel_norm"]) * extent
                ranked = sorted(
                    (abs(anchor_px - float(item["candidate_pixel"])), index, item) for index, item in enumerate(candidates)
                )
            except (KeyError, TypeError, ValueError):
                ranked = []
            if ranked and ranked[0][1] not in used and ranked[0][0] <= snap_limit:
                distance, index, candidate = ranked[0]
                anchor["pixel_norm"] = float(candidate["pixel_norm"])
                used.add(index)
                entry.update(snapped=True, candidate_pixel=int(candidate["candidate_pixel"]),
                             snap_distance_px=float(distance), output=dict(anchor))
            entries.append(entry)
        audit["axes"][axis_id] = {"snap_limit_px": snap_limit, "anchors": entries}
    return reconciled, audit


def apply_extraction_settings(spec, settings, width, height):
    """Copy agent 02's per-series settings onto spec series, clamped to safe ranges."""
    by_label = {_normalise(series.get("label")): series for series in spec.get("series", [])}
    for setting in settings:
        series = by_label.get(_normalise(setting.get("label")))
        if series is None:
            continue
        tolerance = setting.get("color_tolerance")
        series["color_tolerance"] = None if tolerance is None else min(45.0, max(15.0, float(tolerance)))
        series["separate_from"] = [str(label) for label in setting.get("separate_from") or []]
        series["y_bands"] = [dict(band) for band in setting.get("y_bands") or []
                             if band["x_from"] < band["x_to"] and band["y_min"] < band["y_max"]]
        series["template_fill"] = setting.get("template_fill") if setting.get("template_fill") in ("on", "fit_only", "off") else "on"
        series["optimization_group"] = str(setting.get("optimization_group") or f"series:{series.get('label')}")
        series["setting_reason"] = str(setting.get("reason") or "")
        sample = setting.get("sample_marker_norm")
        series["sample_px"] = ([round(float(sample[0]) * width, 1), round(float(sample[1]) * height, 1)]
                               if isinstance(sample, list) and len(sample) == 2 and all(0 <= v <= 1 for v in sample)
                               else None)
        series["expected_counts"] = [dict(item) for item in setting.get("expected_counts") or []
                                     if item["x_from"] < item["x_to"]]
        series["curve_points"] = sorted(
            ({"x": float(p["x"]), "y": float(p["y"])} for p in setting.get("curve_points") or []), key=lambda p: p["x"])


def _validated_axis_bound(axis, value, bound_key):
    """Accept Agent 02 bounds only when they remain close to the printed tick range."""
    if value in (None, ""):
        return None, "not_set"
    try:
        value = float(value)
        ticks = [float(tick) for tick in axis.get("ticks") or []]
        if not math.isfinite(value) or len(ticks) < 2:
            return None, "rejected_unverifiable"
        scale = str(axis.get("scale") or "linear")
        scale_values = [math.log10(tick) if scale == "log" and tick > 0 else tick for tick in ticks]
        if scale == "log" and any(tick <= 0 for tick in ticks):
            return None, "rejected_unverifiable"
        scale_bound = math.log10(value) if scale == "log" and value > 0 else value
        if scale == "log" and value <= 0:
            return None, "rejected_outside_tick_range"
        ordered = sorted(scale_values)
        steps = [right - left for left, right in zip(ordered, ordered[1:]) if right > left]
        if not steps:
            return None, "rejected_unverifiable"
        low, high = ordered[0], ordered[-1]
        overhang = max(low - scale_bound, scale_bound - high, 0.0)
        if overhang > max(steps):
            return None, "rejected_outside_tick_range"
        return value, "accepted"
    except (TypeError, ValueError, OverflowError):
        return None, "rejected_unverifiable"


def apply_axes_check(spec, check, width, height):
    """Merge the complete agent 02 structure into the extractor spec for the next Python round."""
    structure = spec["llm_spec"]
    bound_validation = {}
    for axis_id, report_key in (("x", "x_axis"), ("y", "y_axis")):
        checked = check.get(report_key) or {}
        axis = spec[axis_id]
        for key in ("title", "unit", "scale"):
            if checked.get(key) not in (None, ""):
                axis[key] = checked[key]
        if len(checked.get("ticks") or []) >= 2:
            axis["ticks"] = list(checked["ticks"])
        if len(checked.get("tick_anchors") or []) >= 2:
            axis["tick_anchors"] = list(checked["tick_anchors"])
        bound_validation[axis_id] = {}
        for key in ("minimum", "maximum"):
            axis[key], bound_validation[axis_id][key] = _validated_axis_bound(
                axis, checked.get(key), key,
            )
        axis["boundary_limits_confirmed"] = bool(checked.get("boundary_limits_confirmed", False))
        structure[axis_id] = dict(axis)
    structure["agent02_axis_bounds_validation"] = bound_validation

    legend_norm = valid_bbox_norm(check.get("legend_bbox_norm"))
    if legend_norm is not None:
        spec["legend_bbox"] = norm_to_px(legend_norm, width, height, pad=0.01)
        structure["legend_bbox_norm"] = legend_norm
    else:
        # Optional geometry is supporting evidence; a malformed box must not block calibration.
        structure.pop("legend_bbox_norm", None)

    original = list(structure.get("series") or [])
    checked_series = list(check.get("series") or [])
    if checked_series:
        blank = {"gas": "", "species_evidence": "", "extract": False, "quantity_type": "",
                 "loading_basis": "unknown", "y_anchors": None, "n_markers_estimate": 0}
        original_specs = list(spec.get("series") or [])
        rebuilt_structure, rebuilt_spec = [], []
        for item, match in zip(checked_series, match_checked_series(original, checked_series)):
            prior = dict(original[match]) if match is not None else {**blank, "has_line": item.get("line_style") != "none"}
            prior.update({key: item.get(key, prior.get(key)) for key in STYLE_KEYS})
            prior["label"] = naming.series_label(prior.get("label"))
            prior["has_line"] = prior.get("line_style") != "none"
            rebuilt_structure.append(prior)
            old_spec = dict(original_specs[match]) if match is not None else dict(blank)
            old_spec.update({key: prior.get(key, old_spec.get(key)) for key in STYLE_KEYS})
            rebuilt_spec.append(old_spec)
        structure["series"] = rebuilt_structure
        spec["series"] = rebuilt_spec
    apply_extraction_settings(spec, check.get("extraction_settings") or [], width, height)
    spec["region_strategies"] = [
        {**region, "bbox_px": norm_to_px(box, width, height)}
        for region in check.get("region_strategies") or []
        if (box := valid_bbox_norm(region.get("bbox_norm"))) is not None
    ]
    spec["shared_x_columns"] = sorted(float(value) for value in check.get("shared_x_columns") or [])
    structure["chart_title"] = check.get("chart_title", "")
    structure["branches"] = check.get("branches", structure.get("branches", "unknown"))
    structure["complex_regions"] = list(check.get("complex_regions") or [])
    spec["complex_regions"] = structure["complex_regions"]
    spec["llm_spec"] = structure
    return spec


def _round_scores(panel_dir, through_round=None) -> dict:
    """{round_no: score contents}, including derived detail for legacy snapshots."""
    return load_round_scores(panel_dir, through_round=through_round)


def round_handoff(panel_dir, latest_round_no: int, agent_satisfied: bool, selected_by_series=None) -> dict:
    """Describe the per-series composite handed to independent review."""
    scores = _round_scores(panel_dir, through_round=latest_round_no)
    selected_by_series = selected_by_series or round_score.pick_best_by_series(scores)
    return {
        "selected": latest_round_no,
        "latest": latest_round_no,
        "strategy": "per_series_best_round",
        "selected_by_series": selected_by_series,
        "agent_approved": bool(agent_satisfied),
        "authority": "agent02_source_review_per_series_composite" if agent_satisfied else "per_series_composite_requires_review",
        "scores_role": "per_series_selection",
        "scores": scores,
        "reason": ("Agent 02 approved the plan; Python composed each series from its best extraction round."
                   if agent_satisfied else
                   "Agent 02 requested a targeted patch; Python composed each series from its best round for independent review."),
    }


def _json(value):
    return json.dumps(value, indent=1, default=float)


def current_structure(spec: dict) -> dict:
    """The part of spec.json the agent can correct."""
    return {key: spec.get(key) for key in ("x", "y", "series", "complex_regions")} | {
        "legend_bbox_px": spec.get("legend_bbox"),
        "panel_size_px": spec.get("panel_bbox", [0, 0, 0, 0])[2:],
    }


def application_audit(round_no: int, max_rounds: int, answer: dict, *, state: str, reason: str = "") -> dict:
    """Record whether Agent 02's requested settings reached an extraction.

    Keeping the requested settings in a separate audit prevents later readers
    from confusing an agent recommendation with the spec that produced the
    saved points.  The final targeted patch is always executed once.
    """
    settings = list(answer.get("extraction_settings") or [])
    return {
        "round": int(round_no), "max_rounds": int(max_rounds), "checked": True,
        "state": state, "reason": reason,
        "proposed": {
            "extraction_settings": len(settings),
            "optimization_groups": len({str(item.get("optimization_group") or "") for item in settings}),
            "region_strategies": len(answer.get("region_strategies") or []),
            "shared_x_columns": len(answer.get("shared_x_columns") or []),
            "complex_regions": len(answer.get("complex_regions") or []),
        },
        "applied": state in {"applied_complete_plan", "applied_final_patch"},
        "unexecuted": False,
    }


def python_result(panel_dir, panel: Panel):
    """Status text and JSON details for the latest Python extraction run.

    Agent 02's visual inputs are selected separately by runtime/agent02.yaml.
    """
    images_dir = layout.images_dir(panel_dir, "python")
    tick_candidates = images_dir / "tick_candidates.json"
    summary_path = layout.step_file(panel_dir, "python", "summary.json")
    summary = read_json(summary_path) if summary_path.exists() else {}
    outlier_review = candidate_outlier_failure(summary.get("hard_checks") or {})
    if panel.extract_error.startswith(AXIS_CHECK_FAILED):
        text = (f"Python found the markers, but its NUMBERS FAILED the hard axis check: {panel.extract_error}\n\n"
                "The overlay can look perfect while the tick-to-pixel mapping is shifted or stretched. Compare the "
                "re-plotted axis in `compare.png` with the source, then fix `ticks` and `tick_anchors` "
                "(read the printed label at each detected tick candidate and copy its `pixel_norm`).")
        audit = read_json(layout.step_file(panel_dir, "python", "audit.json"))
        details = {"hard_checks": audit.get("hard_checks", {}),
                   "tick_candidates": read_json(tick_candidates) if tick_candidates.exists() else {},
                   "python_summary": read_json(layout.step_file(panel_dir, "python", "summary.json"))}
        return text, details
    if panel.extract_error:
        text = (f"Python FAILED this round: {panel.extract_error}\n\n"
                + ("Image 2 (`tick_check.png`) marks every tick candidate Python detected; the exact candidates are "
                   "listed below. Map the printed values to them." if tick_candidates.exists()
                   else "Python could not find the axis frame, so there are no tick candidates."))
        details = read_json(tick_candidates) if tick_candidates.exists() else {}
    else:
        text = (
            "Python calibrated the plot frame to the printed ticks, but one or more proposed point values lie "
            "outside the printed axes. Treat those rows as candidates for source review and correct/delete any "
            "unsupported points; the final hard check will still block unresolved outliers. `hard_checks` lists "
            "the affected values."
            if outlier_review else
            "Python succeeded and passed the hard axis check. `hard_checks` gives the printed vs extracted value "
            "range per axis, the redraw score (recall = share of native marker ink covered, per series) and physics "
            "flags; `count_check` compares your expected counts with Python's. Series counts, calibration residuals "
            "and quality warnings:"
        )
        details = summary
    return text, details


class CheckExtraction(PanelStep):
    async def run_panel(self, run: DocumentRun, panel: Panel) -> str:
        panel_dir = run.panel_dir
        max_rounds = load_workflow().max_check_rounds
        panel.check_rounds += 1
        round_no = panel.check_rounds
        spec = read_json(panel_dir / "spec.json")
        status, details = python_result(panel_dir, panel)
        runtime_inputs = resolve_runtime_inputs("agent02", panel_dir)
        runtime_images = [item.path for item in runtime_inputs]
        image_list = "\n".join(
            f"{index}. `{item.upload_name}` (runtime input: `{item.logical_path}`)"
            for index, item in enumerate(runtime_inputs, start=1)
        )
        round_path = layout.round_file(panel_dir, round_no)
        phase = "complete_extraction_plan" if round_no == 1 else "targeted_verification_patch"
        answer = await self.agent.ask(
            {"round": str(round_no), "max_rounds": str(max_rounds), "phase": phase, "image_list": image_list,
             "current_structure": current_structure(spec), "extraction_status": status, "python_result": details},
            images=runtime_images,
            save_to=round_path,
        )
        save_json(layout.step_file(panel_dir, "agent02", "answer.json"), answer)
        axis_failed = [axis for axis in ("x", "y") if answer["axis_check"][axis]["result"] == "fail"]
        satisfied = answer["satisfied"] and not axis_failed  # an agent can not approve numbers it says are wrong

        def save_round(answer, application, tick_reconciliation=None, handoff=None):
            merged = {**answer, "application": application}
            if tick_reconciliation is not None:
                merged["tick_reconciliation"] = tick_reconciliation
            if handoff is not None:
                merged["selected_round"] = handoff
            save_json(round_path, merged)
            save_json(layout.step_file(panel_dir, "agent02", "answer.json"), merged)
            # Store the response beside the exact Python input snapshot it
            # checked, before the next extraction can replace the live spec.
            snapshot = layout.step_dir(panel_dir, "python") / "rounds" / f"round{round_no}"
            if snapshot.is_dir():
                save_json(snapshot / "agent02_answer.json", merged)

        if satisfied and not panel.extract_error:
            composite = compose_best_series_rounds(panel_dir, round_no, panel, getattr(run, "source", ""))
            if not composite["hard_checks"]["passed"]:
                panel.needs_review = True
                panel.message = "Per-series round composition produced Python diagnostic warnings: " + "; ".join(
                    composite["hard_checks"].get("problems") or []
                )
            save_round(answer, application_audit(round_no, max_rounds, answer, state="checked_no_adjustment",
                                                 reason="agent_satisfied"),
                       handoff=round_handoff(
                           panel_dir, round_no, agent_satisfied=True,
                           selected_by_series=composite["selected_round_by_series"],
                       ))
            return CONTINUE
        tick_reconciliation = None
        tick_candidates = layout.image_file(panel_dir, "python", "tick_candidates.json")
        if panel.extract_error and tick_candidates.exists():
            answer, tick_reconciliation = reconcile_retry_tick_anchors(answer, read_json(tick_candidates))
        width, height = spec["panel_bbox"][2], spec["panel_bbox"][3]
        save_json(panel_dir / "spec.json", apply_axes_check(spec, answer, width, height))
        final_patch = round_no >= max_rounds
        state = "applied_final_patch" if final_patch else "applied_complete_plan"
        reason = ("second Agent 02 response saved for one final Python extraction before independent review"
                  if final_patch else "complete plan saved before Python extraction")
        save_round(answer, application_audit(round_no, max_rounds, answer, state=state, reason=reason),
                   tick_reconciliation)
        if final_patch:
            panel.agent02_final_patch = True
            panel.needs_review = True
            panel.message = "Agent 02 verification requested a final targeted Python patch: " + "; ".join(answer["issues"])
        return ADJUST
