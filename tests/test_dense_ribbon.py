"""Dense-region ribbon resolver and the on-ink row diagnostic."""
from __future__ import annotations

import json

import pathlib

import cv2

from src.tools import dense_ribbon, stage_artifacts

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _inputs(saved_panel):
    spec = json.loads((saved_panel / "spec.json").read_text(encoding="utf-8"))
    extraction = json.loads((saved_panel / "python" / "points.json").read_text(encoding="utf-8"))
    return spec, extraction


def test_resolver_adds_cited_points_without_removing_any(saved_panel):
    spec, extraction = _inputs(saved_panel)
    before = {s["label"]: [p["point_id"] for p in s["points"]] for s in extraction["series"]}
    audit = dense_ribbon.resolve(extraction, spec, cv2.imread(spec["image"]))
    assert audit["enabled"] is True
    assert audit["added_total"] >= 0
    for series in extraction["series"]:
        ids = [p.get("point_id") for p in series["points"]]
        assert ids[:len(before[series["label"]])] == before[series["label"]]  # nothing removed or reordered
        for point in series["points"][len(before[series["label"]]):]:
            assert point["source"] == "dense_ribbon"
            assert point["evidence_kind"] in {"partial_marker", "estimated"}
            assert point["uncertainty_px"] > 0 and point["evidence_ref"] == "panel.png"
            assert isinstance(point["x"], float) and isinstance(point["y"], float)
    entry = audit["series"][extraction["series"][0]["label"]]
    assert entry["status"] in {"resolved", "skipped"}


def test_stage_rows_carry_on_ink_flag(saved_panel):
    spec, extraction = _inputs(saved_panel)
    candidate, rows, metadata = stage_artifacts.build_candidate(extraction, spec, "fig3a", "a")
    flagged = {pid for ids in metadata["off_ink_rows"].values() for pid in ids}
    assert all("on_series_ink" in p for s in candidate["series"] for p in s["points"] if p.get("px"))
    for row in rows:
        assert ("on_series_ink=false" in row["notes"]) == (row["point_id"] in flagged)
    # the saved panel has a line_sample tail far from the markers: at least one off-ink row expected
    assert flagged, "expected at least one row off its series colour in the saved panel"


def test_resolver_disabled_without_axis_models(saved_panel):
    spec, extraction = _inputs(saved_panel)
    extraction["calibration"].pop("axis_models", None)
    audit = dense_ribbon.resolve(extraction, spec, cv2.imread(spec["image"]))
    assert audit["enabled"] is False


def test_prune_only_removes_rows_on_another_series_ink(saved_panel):
    spec, extraction = _inputs(saved_panel)
    before = sum(len(s["points"]) for s in extraction["series"])
    audit = dense_ribbon.prune_misassigned(extraction, spec, cv2.imread(spec["image"]))
    after = sum(len(s["points"]) for s in extraction["series"])
    assert before - after == audit["dropped_total"]
    for label, items in audit["dropped"].items():
        for item in items:
            assert item["on_ink_of"] != label and item["on_ink_of"]


def test_ribbon_rows_round_trip_through_the_agent04_validator(saved_panel, tmp_path):
    """Rows the resolver writes must satisfy the final CSV contract unchanged."""
    import copy
    import csv
    import io

    from src.tools import qa_csv

    spec, extraction = _inputs(saved_panel)
    dense_ribbon.resolve(extraction, spec, cv2.imread(spec["image"]))
    candidate, rows, _ = stage_artifacts.build_candidate(extraction, spec, "fig3a", "a")
    marker_rows = [row for row in rows if row["evidence_type"] in qa_csv._MARKER_EVIDENCE_TYPES]
    deletes = [row["point_id"] for row in rows if row["point_id"] not in {r["point_id"] for r in marker_rows}]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=stage_artifacts.STAGE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(marker_rows)
    path = tmp_path / "final.csv"
    path.write_text(buffer.getvalue(), encoding="utf-8")
    report = {
        "verdict": "review",
        "series": [{"label": s["label"], "coverage": "uncertain",
                    "final_count": sum(r["series_name"] == s["label"] for r in marker_rows), "comment": ""}
                   for s in candidate["series"]],
        "row_count": len(marker_rows),
        "changes": [{"action": "delete", "point_id": pid, "reason": "trace row", "source_evidence": "line ink"} for pid in deletes],
        "calibration": copy.deepcopy(candidate["calibration"]),
        "unresolved_slots": {"total": 0, "by_series": {}, "slots": []},
        "uncertainties": [],
    }
    final, out_rows, audit = qa_csv.load_final_csv(path, report, candidate, spec, "fig3a", "a", reconcile_bookkeeping=True)
    assert len(out_rows) == len(marker_rows)
    assert audit.get("rejected_rows", []) == []


def test_contract_documents_every_notes_key_the_host_writes():
    import json

    contract = json.loads((ROOT / "schemas" / "points_csv.contract.json").read_text(encoding="utf-8"))
    documented = set()
    for key in contract["notes_keys"]:
        documented.update(part.strip() for part in key.split(","))
    written = {"branch", "overlap_flag", "mask_warning", "assigned_by", "source", "evidence_kind", "evidence_ref",
               "source_evidence", "uncertainty_px", "segment_id", "trace_order", "on_series_ink",
               "carried_from_agent03", "reviewer_asserted_marker", "origin_estimate"}
    assert written <= documented, written - documented
    assert contract["schema_version"] == "4.2"


def test_example_csv_matches_contract_columns():
    import csv

    with open(ROOT / "examples" / "points.example.csv", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == stage_artifacts.STAGE_COLUMNS
        rows = list(reader)
    sources = {row["notes"] for row in rows}
    assert any("source=dense_ribbon" in note and "evidence_kind=partial_marker" in note for note in sources)
    assert any("source=dense_ribbon" in note and "evidence_kind=estimated" in note for note in sources)
    assert any("on_series_ink=false" in note for note in sources)
    for row in rows:
        if row["evidence_type"] == "estimated_marker":
            assert row["is_inferred"] == "true" and row["confidence"] == "low"
            assert row["uncertainty_x"] and row["uncertainty_y"]
