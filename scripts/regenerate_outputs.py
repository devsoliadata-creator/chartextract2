"""Regenerate old-layout runs (artifacts/runs) into the new panel layout (src/models/layout.py).

Reads an old-layout run tree (flat <panel>/<stage>_points.json etc, see docs/files.md
"Output files" and artifacts/runs/calf20_02/figp1a for a full example) and writes a
new-layout copy under --output, WITHOUT touching --source.  Every picture (overlay,
recreated, compare, redraw_diff, legend sheet, contact sheet) is regenerated with the
current code from the copied points -- this is the whole point of the script, not just a
file shuffle.

  python scripts/regenerate_outputs.py --source artifacts/runs --output artifacts/runs_v4
  python scripts/regenerate_outputs.py --source artifacts/runs --output artifacts/runs_v4 --document calf20_02

Windows-friendly: only pathlib, no POSIX-only shell calls.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import traceback
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.models import layout  # noqa: E402
from src.tools import extract, hard_checks, legend_markers, recreate, table  # noqa: E402


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------

def _load_json(path: Path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _copy(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _copy_tree(src: Path, dst: Path) -> bool:
    if not src.is_dir():
        return False
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if item.is_file():
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    return True


ROUND_RE = re.compile(r"_round(\d+)")


def _round_numbers(panel_old: Path, prefix: str) -> list[int]:
    numbers = set()
    for path in panel_old.glob(f"{prefix}_round*.json"):
        if path.name.endswith(".provenance.json"):
            continue
        match = ROUND_RE.search(path.stem)
        if match:
            numbers.add(int(match.group(1)))
    return sorted(numbers)


# --------------------------------------------------------------------------------------
# step 1: copy an old-layout panel into the new layout (no image regeneration here)
# --------------------------------------------------------------------------------------

def copy_panel(panel_old: Path, panel_new: Path, log: list[dict]) -> dict:
    """Copy one old-layout panel into the new layout. Returns a small report dict."""
    report = {"panel": str(panel_old), "stages": {}, "warnings": []}
    panel_new.mkdir(parents=True, exist_ok=True)

    # top-level files carried over unchanged
    for name in ("panel.png", "spec.json", "qa.json", "error.txt"):  # native.png duplicates panel.png
        _copy(panel_old / name, panel_new / name)
    _copy_tree(panel_old / "evidence", panel_new / "evidence")

    # point the copied spec at the copied panel.png (old specs hold the Windows path of the
    # machine that produced them)
    spec_path = panel_new / "spec.json"
    if spec_path.is_file():
        spec = _load_json(spec_path)
        spec["image"] = str((panel_new / "panel.png").resolve())
        _write_json(spec_path, spec)

    # ---- python stage --------------------------------------------------------------
    if (panel_old / "python_points.json").is_file():
        step = layout.step_dir(panel_new, "python")
        _copy(panel_old / "python_points.json", step / "points.json")
        _copy(panel_old / "python_points.csv", step / "points.csv")
        _copy(panel_old / "python_summary.json", step / "summary.json")
        metadata = _load_json(panel_old / "python_metadata.json") if (panel_old / "python_metadata.json").is_file() else None
        hard = _load_json(panel_old / "python_hard_checks.json") if (panel_old / "python_hard_checks.json").is_file() else None
        extraction_audit = _load_json(panel_old / "python_audit.json") if (panel_old / "python_audit.json").is_file() else None
        _write_json(step / "audit.json", {
            key: value for key, value in {
                "metadata": metadata, "hard_checks": hard, "extraction_audit": extraction_audit,
            }.items() if value is not None
        })
        report["stages"]["python"] = _load_json(panel_old / "python_points.json").get("series", [])
        report["python_row_count"] = len(list(csv_rows(panel_old / "python_points.csv")))

    # ---- agent01 ---------------------------------------------------------------------
    if (panel_old / "agent01_read_chart.json").is_file():
        _copy(panel_old / "agent01_read_chart.json", layout.step_file(panel_new, "agent01", "answer.json"))
        _log_provenance(log, panel_old, "agent01_read_chart", "agent01")

    # ---- agent02 (rounds + latest) ----------------------------------------------------
    rounds = _round_numbers(panel_old, "agent02_check_extraction")
    latest_content = None
    for n in rounds:
        check = panel_old / f"agent02_check_extraction_round{n}.json"
        merged = _load_json(check) if check.is_file() else {}
        application = panel_old / f"agent02_application_round{n}.json"
        if application.is_file():
            merged = {**merged, "application": _load_json(application)}
        tick = panel_old / f"agent02_tick_reconciliation_round{n}.json"
        if tick.is_file():
            merged = {**merged, "tick_reconciliation": _load_json(tick)}
        _write_json(layout.round_file(panel_new, n), merged)
        latest_content = merged
        _log_provenance(log, panel_old, f"agent02_check_extraction_round{n}", "agent02")
    if latest_content is None and (panel_old / "agent02_check_extraction.json").is_file():
        latest_content = _load_json(panel_old / "agent02_check_extraction.json")
    if latest_content is not None:
        _write_json(layout.step_file(panel_new, "agent02", "answer.json"), latest_content)

    # ---- agent03 -----------------------------------------------------------------------
    if (panel_old / "agent03_points.json").is_file():
        step = layout.step_dir(panel_new, "agent03")
        _copy(panel_old / "agent03_points.json", step / "points.json")
        _copy(panel_old / "agent03_points.csv", step / "points.csv")
        _copy(panel_old / "agent03_review_points.json", step / "answer.json")
        metadata = _load_json(panel_old / "agent03_metadata.json") if (panel_old / "agent03_metadata.json").is_file() else None
        hard = _load_json(panel_old / "agent03_hard_checks.json") if (panel_old / "agent03_hard_checks.json").is_file() else None
        edit_audit = _load_json(panel_old / "agent03_edit_audit.json") if (panel_old / "agent03_edit_audit.json").is_file() else None
        recovery = _load_json(panel_old / "agent03_missing_strip_recovery.json") if (panel_old / "agent03_missing_strip_recovery.json").is_file() else None
        _write_json(step / "audit.json", {
            key: value for key, value in {
                "metadata": metadata, "hard_checks": hard, "edit_audit": edit_audit, "recovery": recovery,
            }.items() if value is not None
        })
        report["agent03_row_count"] = len(list(csv_rows(panel_old / "agent03_points.csv")))
        _log_provenance(log, panel_old, "agent03_review_points", "agent03")
        for batch in sorted(panel_old.glob("agent03_review_points_batch_*.json")):
            if batch.name.endswith(".provenance.json"):
                continue
            _log_provenance(log, panel_old, batch.stem, "agent03")
        checkpoint = panel_old / "agent03_review_points.checkpoint.json"
        if checkpoint.is_file():
            log.append({
                "step": "agent03", "kind": "checkpoint", "status": "info",
                "detail": _load_json(checkpoint),
            })
        _log_unstructured_attempts(log, panel_old / "agent03_unstructured", "agent03")

    # ---- agent04 -----------------------------------------------------------------------
    if (panel_old / "agent04_points.json").is_file():
        step = layout.step_dir(panel_new, "agent04")
        _copy(panel_old / "agent04_points.json", step / "points.json")
        _copy(panel_old / "agent04_points.csv", step / "points.csv")
        _copy(panel_old / "agent04_final_check.json", step / "answer.json")
        metadata = _load_json(panel_old / "agent04_metadata.json") if (panel_old / "agent04_metadata.json").is_file() else None
        hard = _load_json(panel_old / "agent04_hard_checks.json") if (panel_old / "agent04_hard_checks.json").is_file() else None
        edit_audit = _load_json(panel_old / "agent04_edit_audit.json") if (panel_old / "agent04_edit_audit.json").is_file() else None
        _write_json(step / "audit.json", {
            key: value for key, value in {
                "metadata": metadata, "hard_checks": hard, "edit_audit": edit_audit,
            }.items() if value is not None
        })
        report["agent04_row_count"] = len(list(csv_rows(panel_old / "agent04_points.csv")))
        _log_provenance(log, panel_old, "agent04_final_check", "agent04")
        _log_unstructured_attempts(log, panel_old / "agent04_unstructured", "agent04")

    for entry in log:
        layout.append_log(panel_new, entry)
    return report


def csv_rows(path: Path):
    import csv
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _log_provenance(log: list[dict], panel_old: Path, stem: str, step_name: str) -> None:
    prov = panel_old / f"{stem}.provenance.json"
    if not prov.is_file():
        return
    data = _load_json(prov)
    log.append({
        "step": step_name, "kind": "provenance", "source_file": prov.name, "status": "ok",
        "created_at": data.get("created_at"),
        "agent_name": data.get("agent_name"), "agent_version": data.get("agent_version"),
        "response_id": data.get("response_id"), "usage": data.get("usage"),
    })


def _log_unstructured_attempts(log: list[dict], unstructured_dir: Path, step_name: str) -> None:
    if not unstructured_dir.is_dir():
        return
    for attempt_dir in sorted(p for p in unstructured_dir.iterdir() if p.is_dir() and p.name.startswith("attempt-")):
        prov_path = attempt_dir / "provenance.json"
        prov = _load_json(prov_path) if prov_path.is_file() else {}
        failure_path = attempt_dir / "failure.json"
        failure = _load_json(failure_path) if failure_path.is_file() else None
        entry = {
            "step": step_name, "kind": "unstructured_attempt", "attempt": attempt_dir.name,
            "status": "failed" if failure is not None else ("ok" if prov else "unknown"),
            "created_at": prov.get("created_at"),
            "agent_name": prov.get("agent_name") or (failure or {}).get("error_type"),
            "agent_version": prov.get("agent_version"),
            "response_id": prov.get("response_id"), "usage": prov.get("usage"),
        }
        if failure is not None:
            entry["error"] = failure.get("error")
        log.append(entry)


# --------------------------------------------------------------------------------------
# step 2: regenerate every picture from the copied points, per stage
# --------------------------------------------------------------------------------------

def regenerate_stage_images(panel_new: Path, stage_name: str, warnings: list[str]) -> None:
    points_path = layout.step_file(panel_new, stage_name, "points.json")
    spec_path = panel_new / "spec.json"
    if not points_path.is_file() or not spec_path.is_file():
        return
    stage = _load_json(points_path)
    spec = _load_json(spec_path)
    images_dir = layout.images_dir(panel_new, stage_name)
    try:
        extract.redraw_overlay(stage, spec, str(images_dir / "overlay.png"))
        recreate.recreate(stage, spec, str(images_dir / "recreated.png"))
        recreate.side_by_side(str(panel_new / "panel.png"), str(images_dir / "recreated.png"),
                              str(images_dir / "compare.png"))
    except Exception as error:  # keep going: partial regeneration beats none
        warnings.append(f"{stage_name}: overlay/recreate/compare failed: {error}")

    try:
        hard = hard_checks.run(stage, spec, panel_new, stage_name)
        audit_path = layout.step_file(panel_new, stage_name, "audit.json")
        audit = _load_json(audit_path) if audit_path.is_file() else {}
        audit["hard_checks"] = hard
        _write_json(audit_path, audit)
    except Exception as error:
        warnings.append(f"{stage_name}: hard_checks failed: {error}")

    if stage_name == "python":
        measured = (stage.get("calibration") or {}).get("legend_markers", [])
        sheet = images_dir / "legend_markers.png"
        sheet.unlink(missing_ok=True)
        if measured:
            try:
                crop = cv2.imread(spec["image"])
                legend_markers.legend_sheet(crop, [m["legend_box_px"] for m in measured], measured,
                                            [m["label"] for m in measured], sheet)
            except Exception as error:
                warnings.append(f"python: legend sheet failed: {error}")

    if stage_name == "agent03":
        try:
            recreate.contact_sheet(str(panel_new / "panel.png"), str(images_dir / "overlay.png"),
                                   str(images_dir / "recreated.png"), str(images_dir / "contact_sheet.png"))
        except Exception as error:
            warnings.append(f"agent03: contact sheet failed: {error}")


def copy_adjudicate_tiles(panel_old: Path, panel_new: Path) -> bool:
    """Old adjudicate/ tiles are model inputs, not regenerable; copy them as agent03/images/tiles/."""
    src = panel_old / "adjudicate"
    if not src.is_dir():
        return False
    dst = layout.images_dir(panel_new, "agent03") / "tiles"
    return _copy_tree(src, dst)


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------

FOLDER_RE = re.compile(r"^fig(p?)(\d+)([a-z]*)$")


def _synthesize_figure(folder_name: str) -> dict:
    """Best-effort figure/page/panel-letter guess from a panel folder name alone.

    Used only when a document's discovery/run_summary.json is missing on disk (older runs),
    so build_navigation/write_dashboard have something to group panels by.  The folder name
    is used as-is for the actual path, so an imperfect guess here only affects the dashboard's
    displayed figure number/page, never the copied tree.
    """
    match = FOLDER_RE.match(folder_name)
    if not match:
        return {"page": 1, "figure_number": None, "caption": None}
    unnumbered, number, _panel = match.groups()
    if unnumbered:
        return {"page": int(number), "figure_number": None, "caption": None}
    return {"page": 1, "figure_number": int(number), "caption": None}


def regenerate_document(document_old: Path, document_new: Path, report: dict) -> None:
    _copy_tree(document_old / "discovery", document_new / "discovery")
    _copy(document_old / "final.csv", document_new / "final.csv")

    copied_panels = []
    for panel_old in sorted(p for p in document_old.iterdir() if p.is_dir() and p.name != "discovery"):
        if not (panel_old / "python_points.json").is_file():
            continue
        panel_new = document_new / panel_old.name
        warnings: list[str] = []
        log: list[dict] = []
        try:
            panel_report = copy_panel(panel_old, panel_new, log)
        except Exception as error:
            report["errors"].append({"panel": str(panel_old), "phase": "copy", "error": str(error),
                                     "traceback": traceback.format_exc()})
            continue
        for stage_name in ("python", "agent03", "agent04"):
            if layout.step_file(panel_new, stage_name, "points.json").is_file():
                regenerate_stage_images(panel_new, stage_name, warnings)
        if (panel_old / "agent03_points.json").is_file():
            copy_adjudicate_tiles(panel_old, panel_new)
        panel_report["warnings"] = warnings
        report["panels"].append(panel_report)
        copied_panels.append(panel_old.name)

    run_summary_path = document_new / "discovery" / "run_summary.json"
    if not run_summary_path.is_file() and copied_panels:
        # Older run, never wrote discovery/run_summary.json - synthesize a minimal one so
        # table.write_master_table can still find and rebuild this document's review pages.
        _write_json(run_summary_path, {
            "source": document_old.name, "title": document_old.name, "run_id": "regenerated",
            "note": "run_summary.json synthesized by scripts/regenerate_outputs.py: the source run had none.",
            "panels": [{"folder": name, "figure": _synthesize_figure(name)} for name in copied_panels],
        })
        report.setdefault("synthesized_run_summary", []).append(document_old.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="artifacts/runs")
    parser.add_argument("--output", default="artifacts/runs_v4")
    parser.add_argument("--document", default=None, help="regenerate only this document folder")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    documents = [args.document] if args.document else sorted(
        p.name for p in source.iterdir()
        if p.is_dir() and (p / "discovery").is_dir()
    )

    report = {"panels": [], "errors": []}
    for name in documents:
        document_old = source / name
        if not document_old.is_dir():
            print(f"== skip: {document_old} does not exist", file=sys.stderr)
            continue
        print(f"== regenerating {name}", file=sys.stderr)
        regenerate_document(document_old, output / name, report)

    print(f"== rebuilding review pages, dashboard and tables under {output}", file=sys.stderr)
    table.write_master_table(output)

    _write_json(output / "_regenerate_outputs_report.json", report)
    n_warn = sum(len(p.get("warnings", [])) for p in report["panels"])
    print(f"== done: {len(report['panels'])} panels, {len(report['errors'])} errors, {n_warn} warnings",
          file=sys.stderr)


if __name__ == "__main__":
    main()
