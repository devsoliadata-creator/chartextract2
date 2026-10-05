"""Agent 04 CSV validation: one bad row or report field must not discard the table."""
from __future__ import annotations

import copy
import csv
import io
import json

import pytest

from src.tools import qa_csv
from src.tools.stage_artifacts import STAGE_COLUMNS


def _load(panel_dir):
    spec = json.loads((panel_dir / "spec.json").read_text(encoding="utf-8"))
    candidate = json.loads((panel_dir / "agent03" / "points.json").read_text(encoding="utf-8"))
    with open(panel_dir / "agent03" / "points.csv", newline="", encoding="utf-8-sig") as handle:
        candidate_rows = list(csv.DictReader(handle))
    return spec, candidate, candidate_rows


def _csv_text(rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=STAGE_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _report(candidate, rows, deletes):
    return {
        "verdict": "review",
        "series": [{"label": series["label"], "coverage": "uncertain",
                    "final_count": sum(row["series_name"] == series["label"] for row in rows),
                    "comment": ""} for series in candidate["series"]],
        "row_count": len(rows),
        "changes": [{"action": "delete", "point_id": point_id, "reason": "legacy trace row",
                     "source_evidence": "line ink only"} for point_id in deletes],
        "calibration": copy.deepcopy(candidate["calibration"]),
        "unresolved_slots": {"total": 0, "by_series": {}, "slots": []},
        "uncertainties": [],
    }


@pytest.fixture
def final_inputs(saved_panel):
    spec, candidate, candidate_rows = _load(saved_panel)
    marker_rows = [dict(row) for row in candidate_rows if row["evidence_type"] != "line_sample"]
    deletes = [row["point_id"] for row in candidate_rows if row["evidence_type"] == "line_sample"]
    assert marker_rows and deletes
    return saved_panel, spec, candidate, candidate_rows, marker_rows, deletes


def _write(panel_dir, rows):
    path = panel_dir / "agent04" / "authored.csv"
    path.write_text(_csv_text(rows), encoding="utf-8")
    return path


def test_clean_table_validates_in_both_modes(final_inputs):
    panel_dir, spec, candidate, candidate_rows, rows, deletes = final_inputs
    path = _write(panel_dir, rows)
    for tolerant in (False, True):
        final, out_rows, audit = qa_csv.load_final_csv(
            path, _report(candidate, rows, deletes), candidate, spec, "fig3a", "a",
            reconcile_bookkeeping=True, tolerate_row_errors=tolerant,
        )
        assert len(out_rows) == len(rows)
        assert audit.get("rejected_rows", []) == []
        assert audit["deleted"] == sorted(deletes)


def test_one_bad_cell_is_fatal_only_in_strict_mode(final_inputs):
    panel_dir, spec, candidate, candidate_rows, rows, deletes = final_inputs
    rows[0]["confidence"] = "High"  # capitalized: invalid
    path = _write(panel_dir, rows)
    report = _report(candidate, rows, deletes)
    with pytest.raises(qa_csv.FinalCSVError):
        qa_csv.load_final_csv(path, report, candidate, spec, "fig3a", "a", reconcile_bookkeeping=True)
    # Tolerant mode: the bad row is an omitted candidate -> one correction request with the exact error.
    with pytest.raises(qa_csv.CandidateOmissionError) as caught:
        qa_csv.load_final_csv(path, report, candidate, spec, "fig3a", "a",
                              reconcile_bookkeeping=True, tolerate_row_errors=True)
    assert caught.value.point_ids == (rows[0]["point_id"],)
    assert caught.value.row_errors and "confidence" in caught.value.row_errors[0]["error"]


def test_carry_over_keeps_agent03_row_for_invalid_candidate_row(final_inputs):
    panel_dir, spec, candidate, candidate_rows, rows, deletes = final_inputs
    bad_id = rows[0]["point_id"]
    rows[0]["confidence"] = "High"
    path = _write(panel_dir, rows)
    final, out_rows, audit = qa_csv.load_final_csv(
        path, _report(candidate, rows, deletes), candidate, spec, "fig3a", "a",
        reconcile_bookkeeping=True, tolerate_row_errors=True,
        candidate_rows=candidate_rows, carry_over_omissions=True,
    )
    assert len(out_rows) == len(rows)
    assert audit["carried_over"] == [bad_id]
    assert audit["rejected_rows"][0]["point_id"] == bad_id
    carried = next(row for row in out_rows if row["point_id"] == bad_id)
    assert "carried_from_agent03=true" in carried["notes"]
    assert carried["confidence"] in {"high", "medium", "low"}
    assert audit["requires_review"] is True
    assert final["agent04_report"]["verdict"] == "review"
    assert {row["point_id"] for row in out_rows} == {
        point["point_id"] for series in final["series"] for point in series["points"]
    }


def test_invalid_new_row_is_dropped_not_fatal(final_inputs):
    panel_dir, spec, candidate, candidate_rows, rows, deletes = final_inputs
    bogus = dict(rows[0])
    bogus.update(point_id="pt-new-0001", source_instance_id="src-new-0001", species="N2")
    path = _write(panel_dir, rows + [bogus])
    final, out_rows, audit = qa_csv.load_final_csv(
        path, _report(candidate, rows, deletes), candidate, spec, "fig3a", "a",
        reconcile_bookkeeping=True, tolerate_row_errors=True,
    )
    assert len(out_rows) == len(rows)
    assert [item["point_id"] for item in audit["rejected_rows"]] == ["pt-new-0001"]


def test_malformed_report_fields_are_normalized(final_inputs):
    panel_dir, spec, candidate, candidate_rows, rows, deletes = final_inputs
    path = _write(panel_dir, rows)
    report = _report(candidate, rows, deletes)
    report["verdict"] = "maybe"
    report["series"] = "not a list"
    report["row_count"] = "many"
    report["unresolved_slots"] = {"total": 5, "by_series": {}, "slots": [
        {"series_label": candidate["series"][0]["label"], "reason": "fused band"}, "junk"]}
    report["changes"].append({"action": "teleport", "point_id": "x"})
    with pytest.raises(qa_csv.FinalCSVError):
        qa_csv.load_final_csv(path, copy.deepcopy(report), candidate, spec, "fig3a", "a",
                              reconcile_bookkeeping=True)
    final, out_rows, audit = qa_csv.load_final_csv(
        path, report, candidate, spec, "fig3a", "a",
        reconcile_bookkeeping=True, tolerate_row_errors=True,
    )
    assert len(out_rows) == len(rows)
    assert final["agent04_report"]["verdict"] == "review"
    assert final["unresolved_slots"]["total"] == 1
    assert len(audit["malformed_changes"]) == 1
    assert len(audit["report_normalizations"]) >= 4
    assert {item["label"] for item in final["agent04_report"]["series"]} == {
        series["label"] for series in candidate["series"]}
