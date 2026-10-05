"""Agent 04 - final check (agent, one call per panel).

Input: the agent03 stage.  The agent starts with its candidate and report,
checks them against the source, and returns a verdict plus any final point operations
(schemas/agent04_final_check.schema.json).
Output: agent04/answer.json, the agent04 stage (agent04/points.csv/json, agent04/audit.json), qa.json
(deterministic sanity checks), and review.html - the page where a human can drag points.
Python axis, frame, collision, mask, and calibration checks remain visible diagnostics. They do
not override Agent 04's valid point inventory or verdict.
Then the workflow moves to the next panel, or to publishing after the last one.
"""

from __future__ import annotations

import copy
import json
import hashlib
import math
import pathlib

from src.models import layout
from src.settings import ROOT
from src.tools import checks, hard_checks, review_page, stage_artifacts, qa_csv
from src.workflow.runtime_inputs import csv_text, describe_runtime_inputs, prepare_runtime_inputs
from src.workflow.state import DONE, REVIEW, DocumentRun, Panel
from src.workflow.steps.base import PanelStep, read_json, save_json
from src.workflow.steps.agent03_review_points import draw_stage, read_rows


def points_csv_contract_fingerprint():
    """Pin the exact final CSV contract used for this Agent 04 audit."""
    path = ROOT / "schemas" / "points_csv.contract.json"
    payload = path.read_bytes()
    return {
        "schema_version": json.loads(payload).get("schema_version"),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def crowded_native_evidence(panel_dir, candidate=None, *, ambiguity_limit=3):
    """Return a bounded list of directly attached native crop mappings for QA."""
    evidence_dir = layout.images_dir(panel_dir, "agent03") / "tiles"
    manifest_path = evidence_dir / "review_tiles.json"
    if not manifest_path.is_file():
        return [], []
    manifest = read_json(manifest_path)
    items = list(manifest.get("review_items") or [])
    strips = [item for item in items if item.get("item_id", "").startswith("crowded_strip:")]
    ambiguities = [item for item in items if item.get("item_type") == "ambiguity"]
    selected = []
    for item in strips + ambiguities[:ambiguity_limit]:
        image_name = item.get("source_native_tile") if item.get("item_type") == "ambiguity" else item.get("image") or item.get("native_tile")
        path = evidence_dir / str(image_name or "")
        if not image_name or not path.is_file():
            continue
        record = {
            "item_id": item.get("item_id"),
            "image": str(image_name),
            "kind": "ambiguity" if item.get("item_type") == "ambiguity" else "crowded_strip",
            "source_bbox_px": item.get("source_bbox_px"),
            "native_scale": item.get("native_scale"),
            "candidate_row_ids": item.get("candidate_row_ids") or [],
            "nearby_candidate_rows": item.get("nearby_candidate_rows") or [],
            "series_context": item.get("series_context") or [],
            "competing_series": item.get("competing_series") or [],
        }
        if candidate is not None and record.get("source_bbox_px") and len(record["source_bbox_px"]) == 4:
            x0, y0, x1, y1 = map(float, record["source_bbox_px"])
            center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
            rows = []
            for series in candidate.get("series", []):
                label = str(series.get("label") or "")
                for point in series.get("points", []):
                    px = point.get("px") or []
                    if len(px) != 2 or px[0] is None or px[1] is None:
                        continue
                    px0, py0 = float(px[0]), float(px[1])
                    if x0 <= px0 <= x1 and y0 <= py0 <= y1:
                        rows.append((
                            ((px0 - center_x) ** 2 + (py0 - center_y) ** 2) ** 0.5,
                            {"point_id": point.get("point_id"), "series_label": label,
                             "x": point.get("x"), "y": point.get("y"),
                             "source_pixel": [round(px0, 2), round(py0, 2)],
                             "evidence_type": point.get("evidence_kind") or point.get("source")},
                        ))
            rows.sort(key=lambda item: item[0])
            limit = 12 if record["kind"] == "crowded_strip" else 6
            record["nearby_candidate_rows"] = [row for _, row in rows[:limit]]
            record["candidate_row_ids"] = [row.get("point_id") for _, row in rows[:limit] if row.get("point_id")]
        selected.append((record, path))
    return [record for record, _ in selected], [path for _, path in selected]


def printed_range_mismatch(spec: dict, answer: dict) -> str:
    """Agent 04 reads the printed tick range itself; say so when it differs from the ticks Python used."""
    problems = []
    for axis in ("x", "y"):
        ticks = [float(v) for v in (spec.get(axis) or {}).get("ticks") or []]
        read = answer["axis_check"][axis]
        if ticks and (abs(read["printed_min"] - min(ticks)) > 1e-9 * max(1.0, abs(min(ticks)))
                      or abs(read["printed_max"] - max(ticks)) > 1e-6 * max(1.0, abs(max(ticks)))):
            problems.append(f"{axis}: agent 04 reads printed {read['printed_min']:g}-{read['printed_max']:g}, "
                            f"Python used {min(ticks):g}-{max(ticks):g}")
    return "; ".join(problems)


def reconcile_series_counts(answer: dict, extraction: dict, audit: dict) -> int:
    """Report actual post-edit CO2 counts and return the number of unapplied operations."""
    previous = {str(item.get("label")): item for item in answer.get("series") or [] if item.get("label")}
    reconciled = []
    for series in extraction.get("series", []):
        label = str(series.get("label"))
        report = previous.get(label, {})
        reconciled.append({
            "label": label,
            "coverage": report.get("coverage", "uncertain"),
            "final_count": len(series.get("points") or []),
            "comment": report.get("comment", ""),
        })
    answer["series"] = reconciled
    unapplied = max(0, int(audit.get("requested", 0)) - int(audit.get("applied", 0)))
    if unapplied:
        for series in answer["series"]:
            series["coverage"] = "uncertain"
            note = "One or more requested point operations were not applied; see the Python edit audit."
            series["comment"] = "; ".join(part for part in (series.get("comment", ""), note) if part)
    return unapplied


def load_inline_final_csv(authored_path, answer, candidate, spec, figure_id, panel_id):
    """Persist and validate the complete CSV embedded in Agent 04's JSON report."""
    csv_text_value = answer.get("points_csv")
    if not isinstance(csv_text_value, str):
        raise qa_csv.FinalCSVError("report.points_csv must be a UTF-8 CSV string")
    csv_bytes = csv_text_value.encode("utf-8")
    authored_path = pathlib.Path(authored_path)
    authored_path.parent.mkdir(parents=True, exist_ok=True)
    authored_path.write_bytes(csv_bytes)
    actual_digest = hashlib.sha256(csv_bytes).hexdigest()
    declared_digest = answer.get("points_csv_sha256")
    # The inline CSV is the authoritative byte stream. Models occasionally
    # calculate its digest before their last CSV edit; bind the report to the
    # bytes we persisted and preserve that correction in the audit below.
    digest_corrected = declared_digest != actual_digest
    if digest_corrected:
        answer["points_csv_sha256"] = actual_digest
    calibration = answer.get("calibration")
    frame = calibration.get("frame_px") if isinstance(calibration, dict) else None
    calibration_fallback = not (
        isinstance(frame, list) and len(frame) == 4
        and isinstance(calibration.get("axis_models"), dict)
    )
    if calibration_fallback:
        answer["calibration"] = copy.deepcopy(candidate.get("calibration") or {})
        answer["verdict"] = "review"
        answer["uncertainties"] = list(answer.get("uncertainties") or []) + [
            "Agent 04 returned an incomplete calibration; the validated Agent 03 calibration was retained."
        ]
    final, rows, audit = qa_csv.load_final_csv(
        authored_path, answer, candidate, spec, figure_id, panel_id,
        reconcile_bookkeeping=True,
    )
    if audit.get("report_reconciliation"):
        reconciled = final.get("agent04_report") or {}
        answer["row_count"] = reconciled.get("row_count", len(rows))
        answer["series"] = copy.deepcopy(reconciled.get("series") or answer.get("series") or [])
        answer["verdict"] = reconciled.get("verdict", "review")
        answer["uncertainties"] = list(reconciled.get("uncertainties") or answer.get("uncertainties") or [])
    audit["csv_selection"] = {
        "path": str(authored_path),
        "sha256": actual_digest,
        "binding": "inline_report_sha256",
    }
    if digest_corrected:
        audit["csv_selection"]["declared_sha256"] = declared_digest
        audit["csv_selection"]["sha256_corrected_by_host"] = True
    if calibration_fallback:
        audit["calibration_fallback"] = "retained_validated_agent03_calibration"
        audit["requires_review"] = True
    return authored_path, final, rows, audit


def source_review_style_overrides(answer):
    """Convert Agent 04's final source-checked styles into stage metadata."""
    overrides = {}
    for item in (answer.get("source_review") or {}).get("series", []):
        label = str(item.get("label") or "")
        marker = item.get("marker")
        if label and marker:
            overrides[label] = {
                "marker_shape": marker,
                "marker_fill": item.get("marker_fill"),
                "source": "agent04_source_review",
            }
    return overrides


def _next_response_attempt(panel_dir):
    """Return a stable per-panel attempt number for inline Agent 04 evidence."""
    try:
        entries = read_json(layout.log_file(panel_dir)).get("entries", [])
    except (OSError, ValueError, TypeError):
        entries = []
    number = 1 + sum(
        entry.get("step") == "agent04" and entry.get("attempt") is not None
        for entry in entries if isinstance(entry, dict)
    )
    return number


class FinalCheck(PanelStep):
    def finish(self, run: DocumentRun, route: str) -> DocumentRun:
        return run.next_panel()

    async def run_panel(self, run: DocumentRun, panel: Panel) -> None:
        panel_dir = run.panel_dir
        spec = read_json(panel_dir / "spec.json")
        candidate = read_json(layout.step_file(panel_dir, "agent03", "points.json"))
        candidate_rows = read_rows(layout.step_file(panel_dir, "agent03", "points.csv"))
        # Offline wiring preview: retain the candidate but never claim source QA passed.
        dry_report = dict(self.agent.example)
        dry_report.update(
            verdict="review", reasoning="Dry run: no source QA performed.",
            calibration=candidate["calibration"], row_count=len(candidate_rows), changes=[],
            unresolved_slots={"total": 0, "by_series": {}, "slots": []},
            series=[{"label": series["label"], "coverage": "uncertain",
                     "final_count": len(series.get("points", [])), "comment": "Dry run only."}
                    for series in candidate["series"]],
            uncertainties=["Dry run: the retained coordinates have not been reviewed."],
        )
        dry_csv = csv_text(candidate_rows)
        dry_report.update(
            points_csv=dry_csv,
            points_csv_sha256=hashlib.sha256(dry_csv.encode("utf-8")).hexdigest(),
        )

        async def ask_for_final(repair_feedback=""):
            attempt = _next_response_attempt(panel_dir)
            cached_path = layout.step_file(panel_dir, "agent04", "answer.json")
            if not repair_feedback and cached_path.is_file():
                try:
                    cached_report = read_json(cached_path)
                except (OSError, ValueError, TypeError):
                    cached_report = None
                # ask_artifacts writes the validated transport report before
                # inline-CSV processing. A retained points_csv therefore marks
                # an interrupted post-response attempt that can be resumed
                # without paying for another identical reviewer call.
                if isinstance(cached_report, dict) and isinstance(cached_report.get("points_csv"), str):
                    return (
                        {"report": cached_report, "artifacts": {"answer.json": cached_path},
                         "provenance": {"resumed_cached_response": True}},
                        attempt,
                        layout.step_dir(panel_dir, "agent04") / "authored" / f"attempt-{attempt:04d}.csv",
                    )
            runtime_inputs = prepare_runtime_inputs("agent04", panel_dir, schema=self.agent.schema)
            result = await self.agent.ask_artifacts(
                {"figure_id": panel.figure_id, "panel_id": panel.panel_id,
                 "runtime_files": describe_runtime_inputs(runtime_inputs),
                 "repair_feedback": repair_feedback},
                files=runtime_inputs, images=[panel_dir / "panel.png"],
                panel_dir=panel_dir, step="agent04",
                report_name="answer.json",
                dry_run_files={"answer.json": json.dumps(dry_report)},
                download_citations=False,
            )
            return result, attempt, layout.step_dir(panel_dir, "agent04") / "authored" / f"attempt-{attempt:04d}.csv"

        result, artifact_attempt, authored_csv_path = await ask_for_final()
        answer = result["report"]
        authored_answers = [copy.deepcopy(answer)]
        repair_fallback = False
        try:
            final_csv, final, authored_rows, audit = load_inline_final_csv(
                authored_csv_path, answer, candidate, spec,
                panel.figure_id, panel.panel_id,
            )
        except qa_csv.CandidateOmissionError as first_omission:
            if result.get("provenance", {}).get("dry_run"):
                raise
            feedback = (
                "Your first final CSV omitted Agent 03 point IDs without declaring explicit deletes: "
                + ", ".join(first_omission.point_ids)
                + ". This is the single correction attempt. Return the complete replacement table again, "
                  "including every omitted ID unless a specific source-backed delete is justified in the "
                  "changes report. Preserve all other verified rows and include every Agent 04 addition."
            )
            result, artifact_attempt, authored_csv_path = await ask_for_final(feedback)
            answer = result["report"]
            authored_answers.append(copy.deepcopy(answer))
            try:
                final_csv, final, authored_rows, audit = load_inline_final_csv(
                    authored_csv_path, answer, candidate, spec,
                    panel.figure_id, panel.panel_id,
                )
                audit["correction_attempt"] = {
                    "trigger": list(first_omission.point_ids), "resolved": True,
                }
            except qa_csv.CandidateOmissionError as unresolved_omission:
                # A second incomplete CSV cannot erase Agent 03 inventory. Keep
                # the candidate table intact, make the panel explicitly repairable,
                # and preserve both authored reports and the missing-ID evidence.
                repair_fallback = True
                final = copy.deepcopy(candidate)
                authored_rows = copy.deepcopy(candidate_rows)
                final_csv = authored_csv_path
                csv_digest = (hashlib.sha256(final_csv.read_bytes()).hexdigest()
                              if final_csv.is_file() else None)
                audit = {
                    "authoritative_csv": False,
                    "candidate_count": len(candidate_rows),
                    "final_count": len(candidate_rows),
                    "added": [], "deleted": [], "moved": [], "reassigned": [],
                    "requested": 0, "applied": 0,
                    "requires_review": True,
                    "repair_required": True,
                    "omitted_candidate_ids": list(unresolved_omission.point_ids),
                    "correction_attempt": {
                        "trigger": list(first_omission.point_ids), "resolved": False,
                        "attempts": 1,
                    },
                    "authored_reports": authored_answers,
                    "csv_selection": {
                        "path": str(final_csv) if final_csv else None,
                        "sha256": csv_digest,
                        "binding": "rejected_for_unresolved_candidate_omissions",
                    },
                }
                candidate_counts = {
                    str(series.get("label")): len(series.get("points") or [])
                    for series in candidate.get("series") or []
                }
                answer["verdict"] = "review"
                answer["reasoning"] = str(answer.get("reasoning") or "") + (
                    " Agent 04 still omitted candidate rows after one correction attempt; "
                    "the workflow retained the Agent 03 inventory and marked this panel for repair."
                )
                answer["series"] = [
                    {**item, "coverage": "uncertain",
                     "final_count": candidate_counts.get(item.get("label"), 0),
                     "comment": "; ".join(part for part in (
                         str(item.get("comment") or ""),
                         "Agent 03 inventory retained after unresolved Agent 04 omissions; repair required.",
                     ) if part)}
                    for item in answer.get("series") or []
                ]
                answer["row_count"] = len(candidate_rows)
                answer["changes"] = []
                answer["calibration"] = copy.deepcopy(candidate.get("calibration") or answer["calibration"])
                for axis, value_key in (("x", "x"), ("y", "y")):
                    values = []
                    for row in candidate_rows:
                        try:
                            value = float(row.get(value_key, ""))
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(value):
                            values.append(value)
                    answer["axis_check"][axis]["data_min"] = min(values) if values else None
                    answer["axis_check"][axis]["data_max"] = max(values) if values else None
                answer["unresolved_slots"] = copy.deepcopy(
                    candidate.get("unresolved_slots") or {"total": 0, "by_series": {}, "slots": []}
                )
                answer["uncertainties"] = list(answer.get("uncertainties") or []) + [
                    "Agent 04 omitted candidate IDs after one correction attempt: "
                    + ", ".join(unresolved_omission.point_ids)
                    + ". The Agent 03 inventory is retained until the final table is repaired."
                ]
                final["agent04_report"] = copy.deepcopy(answer)
                panel.needs_review = True

        authored_answer = authored_answers[-1]
        # An incomplete inventory cannot be accepted just because its file parses.
        incomplete = any(item["coverage"] != "complete" for item in answer["series"])
        if (answer["unresolved_slots"]["total"] or incomplete or audit.get("requires_review")) and answer["verdict"] == "accept":
            answer["verdict"] = "review"
            answer["uncertainties"].append("Incomplete source coverage or inferred rows require review.")
        audit["authored_report"] = authored_answer
        if len(authored_answers) > 1:
            audit["authored_reports"] = authored_answers
        # This checksum binds the inline authored CSV, not the stage CSV that
        # is reserialized with normalized descriptive cells. Keep that binding
        # in the audit and do not attach it to a different byte stream.
        answer.pop("points_csv", None)
        answer.pop("points_csv_sha256", None)
        final["agent04_report"] = copy.deepcopy(answer)
        save_json(layout.step_file(panel_dir, "agent04", "answer.json"), answer)
        agent_axis_fail = [axis for axis in ("x", "y") if answer["axis_check"][axis]["result"] == "fail"]
        status = "partial_review_required" if panel.needs_review else None

        def write(status):
            stage, generated_rows, metadata = stage_artifacts.write_stage(
                panel_dir, "agent04", final, spec, panel.figure_id, panel.panel_id,
                source_pdf=run.source, agent_report=answer, edit_audit=audit,
                # A panel Agent 02 never approved remains subject to review.
                status=status, style_overrides=source_review_style_overrides(answer),
            )
            if {row["point_id"] for row in generated_rows} != {row["point_id"] for row in authored_rows}:
                raise ValueError("Stage rebuilding changed the validated QA row inventory")
            # Preserve the validated CSV cells, including notes, uncertainty,
            # and normalized columns; source-backed style reconciliation is audited.
            stage_artifacts._write_csv(layout.step_file(panel_dir, "agent04", "points.csv"), authored_rows)
            # The reviewer supplied a complete replacement table.  Do not
            # derive traces from rows it removed or add anything after CSV QA.
            trace_audit = {
                "disabled": "Agent 04 points.csv is authoritative; deleted rows are not demoted or restored.",
                "trace_sample_count": 0,
            }
            metadata.update(authority=("agent03_inventory_repair_fallback" if repair_fallback
                                       else "agent04_inline_csv"),
                             repair_required=repair_fallback, response_attempt=artifact_attempt,
                             row_count=len(authored_rows),
                             authored_csv=str(final_csv),
                             authored_csv_sha256=audit["csv_selection"]["sha256"],
                             stage_csv_sha256=hashlib.sha256(
                                 layout.step_file(panel_dir, "agent04", "points.csv").read_bytes()
                             ).hexdigest(),
                             points_csv_contract=points_csv_contract_fingerprint(),
                             curve_trace_audit=trace_audit)
            return stage, authored_rows, metadata

        stage, rows, metadata = write(status)
        hard = hard_checks.run(stage, spec, panel_dir, "agent04")
        draw_stage(panel_dir, "agent04", stage, spec)
        stage_artifacts.write_audit(panel_dir, "agent04", metadata=metadata, hard_checks=hard, edit_audit=audit)

        report = checks.run(stage)
        report.update(role="supporting_diagnostics_only", agent04_verdict=answer["verdict"],
                      rows={**panel.rows, "agent04": len(rows)}, final_operations_applied=audit.get("applied", len(audit.get("operations", []))),
                      hard_checks={"passed": hard["passed"], "problems": hard["problems"], "summary": hard_checks.summary_line(hard)})
        save_json(panel_dir / "qa.json", report)
        accepted = metadata.get("status") == "complete_against_reviewed_evidence"
        panel.rows["agent04"] = len(rows)
        tick_mismatch = printed_range_mismatch(spec, answer)
        if tick_mismatch:  # agent 04 reads different tick labels than Python used: a human must look
            panel.needs_review = True
        panel.verdict = answer["verdict"]
        panel.status = DONE if accepted and not panel.needs_review else REVIEW
        panel.message = "; ".join([panel.message] * panel.needs_review + list(answer.get("uncertainties") or []))
        if tick_mismatch:
            panel.message = "; ".join(part for part in (panel.message, tick_mismatch) if part)
        if agent_axis_fail:
            panel.message = "; ".join(part for part in (
                panel.message,
                "Agent 04 axis diagnostic disagrees (" + ", ".join(agent_axis_fail) + "): "
                + "; ".join(answer["axis_check"][axis]["reason"] for axis in agent_axis_fail),
            ) if part)
        if not hard["passed"]:
            panel.message = "; ".join(part for part in (
                panel.message,
                "Python hard-check warnings (Agent 04 inventory retained): " + "; ".join(hard["problems"]),
            ) if part)
        try:
            review_page.build(
                str(layout.step_file(panel_dir, "agent04", "points.json")),
                str(panel_dir / "panel.png"), str(panel_dir / "review.html"),
                comparison_image="agent04/images/compare.png",
                comparison_label="Agent 04 final reconstruction",
                additional_comparison_image="agent03/images/compare.png",
                status=metadata.get("status"),
                status_reason="Agent 04 accepted the final extraction." if accepted
                else "The final extraction remains available for human review.",
            )
        except Exception as error:
            # The table's validated point status is independent of review UI rendering.
            layout.append_log(panel_dir, {
                "step": "agent04_review_page", "status": "failed", "error": str(error),
            })
            panel.message = "; ".join(part for part in (
                panel.message, f"Review page could not be rendered: {error}",
            ) if part)
