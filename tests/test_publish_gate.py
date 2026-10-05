"""The master-table publish gate keeps validated Agent 04 tables instead of discarding them."""
from __future__ import annotations

import csv
import json

from src.tools import table


def _audit(panel_dir):
    return json.loads((panel_dir / "agent04" / "audit.json").read_text(encoding="utf-8"))


def _write_audit(panel_dir, audit):
    (panel_dir / "agent04" / "audit.json").write_text(json.dumps(audit), encoding="utf-8")


def test_saved_panel_publishes_marker_rows_and_drops_only_legacy_traces(saved_panel):
    rows, source, flags = table._final_rows(saved_panel)
    assert rows, source
    assert flags["dropped_legacy_trace_rows"] > 0
    assert all(row["evidence_type"] != "line_sample" or row["marker_shape"] == "none" for row in rows)
    assert flags["reason"] == ""


def test_current_inline_authority_is_accepted(saved_panel):
    audit = _audit(saved_panel)
    audit["metadata"]["authority"] = "agent04_inline_csv"
    _write_audit(saved_panel, audit)
    rows, _, flags = table._final_rows(saved_panel)
    assert rows and flags["reason"] == ""


def test_unknown_authority_is_rejected_with_a_reason(saved_panel):
    audit = _audit(saved_panel)
    audit["metadata"]["authority"] = "something_else"
    _write_audit(saved_panel, audit)
    rows, source, flags = table._final_rows(saved_panel)
    assert rows == [] and "authority" in flags["reason"] and flags["reason"] in source


def test_failed_hard_checks_keep_the_table_but_flag_review(saved_panel):
    audit = _audit(saved_panel)
    audit["hard_checks"]["passed"] = False
    _write_audit(saved_panel, audit)
    rows, _, flags = table._final_rows(saved_panel)
    assert rows and flags["hard_checks_failed"] is True


def test_contract_minor_version_change_does_not_invalidate(saved_panel):
    audit = _audit(saved_panel)
    current = table._points_csv_contract_fingerprint()
    audit["metadata"]["points_csv_contract"] = {"schema_version": "4.0", "sha256": "0" * 64}
    _write_audit(saved_panel, audit)
    rows, _, flags = table._final_rows(saved_panel)
    assert rows and flags["reason"] == ""
    assert table._contract_version_compatible({"schema_version": "4.0"}, current)
    assert not table._contract_version_compatible({"schema_version": "3.9"}, {"schema_version": "4.1"})


def test_aggregation_reports_review_rows_for_saved_panel(saved_panel, monkeypatch):
    out_dir = saved_panel.parent.parent
    monkeypatch.setenv("CHART_EXTRACT_REVIEWS_DIR", str(out_dir / "reviews"))
    table.write_stage_aggregations(out_dir)
    report = json.loads((out_dir / "aggregation_report.json").read_text(encoding="utf-8"))
    assert report["panel_count"] == 1
    panel = report["panels"][0]
    assert panel["status"] == "partial_review_required"
    assert panel["review_rows"] > 0
    assert panel["final_flags"]["dropped_legacy_trace_rows"] > 0
    with open(out_dir / "all_data_review.csv", newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == panel["review_rows"]
