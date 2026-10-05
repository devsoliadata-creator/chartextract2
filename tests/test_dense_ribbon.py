"""Dense-region ribbon resolver and the on-ink row diagnostic."""
from __future__ import annotations

import json

import cv2

from src.tools import dense_ribbon, stage_artifacts


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
