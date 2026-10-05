"""
Build the master table: ONE csv for all documents, one row per data point.

  artifacts/runs/all_data.csv           accepted + review union, deduplicated by point_id
  artifacts/runs/all_data_accepted.csv  agent 04 final rows when its verdict is accept
  artifacts/runs/all_data_review.csv    latest final/candidate rows still requiring review
  artifacts/runs/aggregation_report.json

All four files are rebuilt from agent03/points.csv / agent04/points.csv on disk, so reruns are
idempotent.  The older row builder below remains temporarily for dashboard and
manual-review compatibility; it is not the authority for the aggregate CSVs.

figure_id: f01a = figure 1, panel a · f05 = figure 5, no panel · fp07 = unnumbered figure on page 7
human_requires: True when the point is low-confidence / overlapping, or the panel did not pass QA
(code verdict != auto_accept, or the AI reviewer's verdict != accept).

  python table.py artifacts/runs               # rebuild the master table from all run outputs
"""

import csv
import hashlib
import json
import pathlib
import re
import sys

import cv2
import numpy as np

from src.models import layout, naming
from src.settings import CFG, path_of
from src.tools.stage_artifacts import AGGREGATION_COLUMNS, STAGE_COLUMNS
from src.tools import trace_paths as trace_utils

CONF_OK = CFG["extract"]["confidence_ok"]
STYLESHEET_PATH = (
    pathlib.Path(__file__).resolve().parent / "review.css"
)

COLUMNS = [
    "document_title",
    "run_id",
    "document",
    "page",
    "figure_id",
    "title",
    "series_label",
    "axis_y",
    "axis_x",
    "context_color",
    "confidence",
    "human_requires",
    # extras
    "y_unit",
    "x_unit",
    "branch",
    "temperature_k",
    "panel_verdict_code",
    "panel_verdict_ai",
    "review_html",
    "source",
]


def figure_id(fig, panel_letter):
    return naming.canonical_figure_id(fig, panel_letter)


def figure_title(caption):
    """'Fig. 2 | Adsorption and desorption properties of TAMOF-1. a ...' -> 'Adsorption and desorption properties of TAMOF-1'"""
    if not caption:
        return ""
    t = re.sub(r"^\s*Fig(?:ure|\.)\s*\d+\s*[|.:]?\s*", "", caption.replace("\n", " "))
    return re.split(r"(?<=[a-z0-9\)])\.\s", t, 1)[0].strip()[:160]


def lab_to_hex(lab):
    px = np.uint8([[[round(v) for v in lab]]])
    b, g, r = cv2.cvtColor(px, cv2.COLOR_LAB2BGR)[0, 0]
    return f"#{r:02x}{g:02x}{b:02x}"


def _series_color(series, names):
    """Return the best legacy dashboard color without requiring Lab data."""
    label = str(series.get("label") or "")
    color_name = str(names.get(label) or "")
    lab = series.get("color_lab")
    color_hex = lab_to_hex(lab) if lab else str(series.get("color_hex") or "")
    return " ".join(part for part in (color_name, color_hex) if part).strip()


def panel_id_of(document, pdir):
    return naming.review_file(document, pdir.name).replace(
        ".json", ""
    )


def apply_review(ext, document, pdir):
    """Apply marker-only review edits while keeping readonly trace rows intact."""
    fname = naming.review_file(document, pdir.name)
    candidates = [
        path_of("reviews") / fname,
        pdir.parent.parent / "reviews" / fname,
        pdir / fname,
        pdir / "reviewed.json",
    ]
    f = next((c for c in candidates if c.exists()), None)
    if f is None:
        return ext, False
    rev = json.load(open(f, encoding="utf-8"))
    by_label = {s["label"]: s for s in rev.get("series", [])}
    for s in ext["series"]:
        if s["label"] not in by_label:
            continue
        original_trace_rows = [
            p for p in s.get("points", []) if trace_utils.is_trace_point(p)
        ]
        submitted_trace_rows = by_label[s["label"]].get("trace_rows") or []
        trace_rows = submitted_trace_rows or original_trace_rows
        pts = []
        for p in by_label[s["label"]]["points"]:
            if p.get("point_id") and any(
                trace.get("point_id") == p.get("point_id") for trace in trace_rows
            ):
                continue
            pts.append(
                {
                    "x": p["x"],
                    "y": p["y"],
                    "px": [None, None],
                    "branch": p.get("branch", ""),
                    "confidence": 1.0
                    if p.get("manually_adjusted")
                    else (
                        p.get("confidence") if p.get("confidence") is not None else 1.0
                    ),
                    "overlap_flag": False,
                    "manual": bool(p.get("manually_adjusted")),
                    "review_evidence_status": p.get("evidence_status") or "manual",
                }
            )
        s["points"] = pts + trace_rows
        s["n_points"] = len(pts)
        s["n_line_samples"] = len(trace_rows)
    return ext, True


def rows_for_panel(fig, pdir, doc_title, run_id, document):
    extraction_path = next(
        (
            path
            for path in (layout.step_file(pdir, "agent04", "points.json"),
                        layout.step_file(pdir, "agent03", "points.json"), pdir / "extraction.json")
            if path.exists()
        ),
        None,
    )
    if extraction_path is None:
        raise FileNotFoundError(f"No canonical extraction JSON exists in {pdir}")
    ext = json.load(open(extraction_path, encoding="utf-8"))
    ext, reviewed = apply_review(ext, document, pdir)
    spec = json.load(open(pdir / "spec.json", encoding="utf-8"))
    qa = json.load(open(pdir / "qa.json", encoding="utf-8")) if (pdir / "qa.json").exists() else {}
    a4_path = layout.step_file(pdir, "agent04", "answer.json")
    a4 = json.load(open(a4_path, encoding="utf-8")) if a4_path.exists() else {}
    panel_letter = (spec.get("panel_info") or {}).get("panel", "")
    fid = figure_id(fig, panel_letter)
    title = figure_title(fig.get("caption"))
    v_code, v_ai = qa.get("verdict", ""), a4.get("verdict", "")
    panel_ok = v_code == "auto_accept" and (v_ai in ("accept", ""))
    names = {
        s["label"]: s.get("color_name", "")
        for s in spec.get("llm_spec", {}).get("series", [])
    }
    temps = {
        s["label"]: s.get("temperature_k")
        for s in spec.get("llm_spec", {}).get("series", [])
    }
    rows = []
    for s in ext["series"]:
        color = _series_color(s, names)
        for p in s["points"]:
            if trace_utils.is_trace_point(p):
                continue
            needs_human = (not reviewed) and (
                (not panel_ok)
                or p["confidence"] < CONF_OK
                or p.get("overlap_flag", False)
            )
            rows.append(
                {
                    "document_title": doc_title,
                    "run_id": run_id,
                    "document": document,
                    "page": fig["page"],
                    "figure_id": fid,
                    "title": title,
                    "series_label": s["label"],
                    "axis_y": p["y"],
                    "axis_x": p["x"],
                    "context_color": color,
                    "confidence": p["confidence"],
                    "human_requires": needs_human,
                    "y_unit": spec["y"].get("unit", ""),
                    "x_unit": spec["x"].get("unit", ""),
                    "branch": p.get("branch", ""),
                    "temperature_k": temps.get(s["label"]),
                    "panel_verdict_code": v_code,
                    "panel_verdict_ai": v_ai,
                    "review_html": str(pdir / "review.html"),
                    "source": "manual"
                    if p.get("manual")
                    else (
                        "reviewed"
                        if reviewed
                        else (
                            p["source"]
                            if p.get("source") in ("curve", "grid", "ai_proposed")
                            else ("ai" if p.get("assigned_by") == "ai" else "auto")
                        )
                    ),
                }
            )
    return rows


def rows_for_document(pdf_out_dir):
    """Dashboard rows for one document, driven by the discovery/run_summary.json that python_publish_results writes."""
    d = pathlib.Path(pdf_out_dir)
    if not (d / "discovery" / "run_summary.json").exists():
        return []
    summary = json.load(open(d / "discovery" / "run_summary.json", encoding="utf-8"))
    rows = []
    for panel in summary.get("panels", []):
        pdir = d / panel["folder"]
        if layout.step_file(pdir, "agent03", "points.json").exists() or layout.step_file(pdir, "agent04", "points.json").exists():
            rows.extend(
                rows_for_panel(
                    panel["figure"],
                    pdir,
                    summary.get("title") or d.name,
                    summary.get("run_id", ""),
                    d.name,
                )
            )
    return rows


def write_master_table(out_dir):
    out_dir = pathlib.Path(out_dir)
    path_of("reviews").mkdir(parents=True, exist_ok=True)

    # The staged CSVs are the aggregation authority.  A targeted rerun must
    # not fail because an older document has a legacy extraction JSON that no
    # longer satisfies the dashboard schema (for example, Agent 04-added
    # series without ``color_lab``).  Build the authoritative files first;
    # legacy rows only enrich the optional navigation/dashboard view.
    out, rows = write_stage_aggregations(out_dir)
    legacy_rows = []
    for d in sorted(
        p
        for p in out_dir.iterdir()
        if p.is_dir()
        and p.name != path_of("reviews").name
        and not p.name.startswith(".")
    ):
        try:
            legacy_rows.extend(rows_for_document(d))
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            print(
                f"== warning: skipped legacy dashboard rows for {d}: {error}",
                file=sys.stderr,
            )
    build_navigation(out_dir, legacy_rows)
    return out, len(rows)


def _read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _points_csv_contract_fingerprint():
    """Return the locally enforced final-CSV contract identity."""
    path = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "points_csv.contract.json"
    payload = path.read_bytes()
    return {
        "schema_version": json.loads(payload).get("schema_version"),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _invalid_legacy_trace_rows(rows):
    """Return malformed trace rows authored before the no-marker trace contract.

    These cached reviewer outputs cannot be normalized safely: their original
    source support and intended trace identity are unknown.  Keep them on disk
    for inspection, but do not republish them.
    """
    invalid = []
    for row in rows:
        if str(row.get("evidence_type", "")).strip() != "line_sample":
            continue
        if (str(row.get("marker_shape", "")).strip().lower() != "none"
                or str(row.get("is_inferred", "")).strip().lower() != "true"
                or str(row.get("confidence", "")).strip().lower() != "low"):
            invalid.append(str(row.get("point_id") or "<missing point_id>"))
    return invalid


def _final_rows(panel_dir):
    """Read only a validated Agent 04 stage committed alongside its audit."""
    path = layout.step_file(panel_dir, "agent04", "points.csv")
    audit_path = layout.step_file(panel_dir, "agent04", "audit.json")
    if not path.exists() or not audit_path.exists():
        return [], str(path)
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError):
        return [], str(path)
    metadata = audit.get("metadata") or {}
    if (metadata.get("authority") not in {
                "agent04_code_interpreter_csv", "agent03_inventory_repair_fallback",
            }
            or metadata.get("status") not in {
                "complete_against_reviewed_evidence", "partial_review_required",
            }
            or not (audit.get("hard_checks") or {}).get("passed")):
        return [], str(path)
    log_path = layout.log_file(panel_dir)
    try:
        attempts = [entry for entry in (json.loads(log_path.read_text(encoding="utf-8")).get("entries") or [])
                    if isinstance(entry, dict) and entry.get("step") == "agent04"
                    and entry.get("attempt") is not None] if log_path.exists() else []
    except (OSError, ValueError, TypeError):
        attempts = []
    attempt_dirs = sorted((path.parent / "cited").glob("attempt-*") if (path.parent / "cited").exists() else [])
    if attempt_dirs and attempts:
        latest_attempt = len(attempts)
        if metadata.get("artifact_attempt") != latest_attempt:
            return [], str(path)
    rows = _read_csv(path)
    if metadata.get("row_count") != len(rows):
        return [], str(path)
    fingerprint = metadata.get("points_csv_contract")
    if fingerprint is not None and fingerprint != _points_csv_contract_fingerprint():
        return [], f"{path}: points CSV contract fingerprint changed; rerun Agent 04"
    invalid_traces = _invalid_legacy_trace_rows(rows)
    if invalid_traces:
        return [], (
            f"{path}: legacy line_sample rows violate the current trace contract "
            f"({len(invalid_traces)} rows; rerun Agent 04)"
        )
    return rows, str(path)


def _agent04_attempted(panel_dir, final_metadata):
    """Whether Agent 04 has an uncommitted or invalid attempt for this panel."""
    if final_metadata:
        return True
    try:
        log = json.loads(layout.log_file(panel_dir).read_text(encoding="utf-8"))
        if any(entry.get("step") == "agent04" for entry in log.get("entries", []) if isinstance(entry, dict)):
            return True
    except (OSError, ValueError, TypeError):
        pass
    agent04_dir = layout.step_dir(panel_dir, "agent04")
    return ((agent04_dir / "points.csv").exists()
            or any((agent04_dir / "cited").rglob("*.csv"))
            or any((agent04_dir / "cited").rglob("*.json")))


def _write_rows(path, rows):
    tmp = pathlib.Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=AGGREGATION_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _enrich_stage_row(row, source_pdf, review_html):
    return {
        "source_pdf": source_pdf,
        **{column: row.get(column, "") for column in STAGE_COLUMNS},
        "review_html": review_html,
    }


def _human_point_id(panel_key, series_name, point, index):
    fingerprint = "|".join(
        [
            str(panel_key),
            str(series_name),
            str(index),
            str(point.get("x", "")),
            str(point.get("y", "")),
            str(point.get("branch", "")),
        ]
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"pt-human-{digest}", f"src-human-{digest}"


def overlay_reviewed_rows(rows, review, panel_key="panel"):
    """Apply the review-page point set to staged rows while preserving stable IDs."""
    reviewed_series = {
        str(series.get("label")): series
        for series in review.get("series", [])
        if isinstance(series, dict) and str(series.get("label", ""))
    }
    if not reviewed_series:
        return rows

    grouped = {}
    series_order = []
    for row in rows:
        label = str(row.get("series_name", ""))
        if label not in grouped:
            grouped[label] = []
            series_order.append(label)
        grouped[label].append(row)

    output = []
    for label in series_order:
        originals = grouped[label]
        reviewed = reviewed_series.get(label)
        if reviewed is None:
            output.extend(originals)
            continue

        trace_rows = [
            row for row in originals
            if str(row.get("evidence_type", "")).strip() == "line_sample"
        ]
        marker_originals = [row for row in originals if row not in trace_rows]
        trace_ids = {row.get("point_id") for row in trace_rows if row.get("point_id")}
        by_id = {row.get("point_id"): row for row in marker_originals if row.get("point_id")}
        used_ids = set()
        for index, point in enumerate(reviewed.get("points", [])):
            if not isinstance(point, dict):
                continue
            requested_id = point.get("point_id")
            if requested_id in trace_ids:
                continue
            template = by_id.get(requested_id)
            preserve_existing_id = template is not None
            if template is None and index < len(marker_originals):
                fallback = marker_originals[index]
                if fallback.get("point_id") not in used_ids:
                    template = fallback
                    preserve_existing_id = True
            if template is None and marker_originals:
                template = marker_originals[0]
            if template is None:
                continue

            row = dict(template)
            existing_id = row.get("point_id") if preserve_existing_id else None
            if not existing_id or existing_id in used_ids:
                existing_id, source_instance_id = _human_point_id(
                    panel_key,
                    label,
                    point,
                    index,
                )
                row["point_id"] = existing_id
                row["source_instance_id"] = source_instance_id
            used_ids.add(existing_id)

            row["x"] = point.get("x", "")
            row["y"] = point.get("y", "")
            row["confidence"] = "high"
            row["is_inferred"] = "false"
            row["evidence_type"] = "human_review"
            branch = str(point.get("branch") or "manual")
            row["notes"] = f"branch={branch}; source=human_review"
            if point.get("manually_adjusted") or requested_id not in by_id:
                row["source_pixel_x"] = ""
                row["source_pixel_y"] = ""
                row["normalized_x"] = ""
                row["normalized_y"] = ""
                row["conversion_id"] = ""
            output.append(row)
        output.extend(trace_rows)
    return output


def _load_panel_review(out_dir, panel_dir):
    try:
        relative = panel_dir.relative_to(out_dir)
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    document = relative.parts[0]
    filename = naming.review_file(document, panel_dir.name)
    candidates = [
        out_dir / path_of("reviews").name / filename,
        path_of("reviews") / filename,
    ]
    review_path = next((path for path in candidates if path.exists()), None)
    if review_path is None:
        return None
    review = json.load(open(review_path, encoding="utf-8"))
    return review if review.get("reviewed") is True else None


def regenerate_review_images(panel_dir):
    """Rebuild agent04's overlay/recreated/compare pictures from the reviewer-corrected points.

    review.html must show the same picture a saved review produced, not the stale agent 04
    one.  The untouched agent 04 originals are kept once, as ``*_agent04.png``, so the audit
    trail survives repeated saves.  Returns the regenerated file paths, or [] when there is
    nothing to rebuild (no agent04 stage yet, or no saved review for this panel).
    """
    import datetime
    import shutil

    from src.tools import extract, recreate

    panel_dir = pathlib.Path(panel_dir)
    points_path = layout.step_file(panel_dir, "agent04", "points.json")
    spec_path = panel_dir / "spec.json"
    if not points_path.is_file() or not spec_path.is_file():
        return []
    review = _load_panel_review(panel_dir.parent.parent, panel_dir)
    if review is None:
        return []
    from src.tools.review_page import _saved_review_result

    result = _saved_review_result(json.load(open(points_path, encoding="utf-8")), review)
    spec = json.load(open(spec_path, encoding="utf-8"))
    images_dir = layout.images_dir(panel_dir, "agent04")
    for name in ("compare.png", "recreated.png"):
        current = images_dir / name
        backup = images_dir / f"{pathlib.Path(name).stem}_agent04.png"
        if current.is_file() and not backup.is_file():
            shutil.copyfile(current, backup)
    extract.redraw_overlay(result, spec, str(images_dir / "overlay.png"))
    recreate.recreate(result, spec, str(images_dir / "recreated.png"),
                      evidence_view=True)
    recreate.side_by_side(str(panel_dir / "panel.png"), str(images_dir / "recreated.png"),
                          str(images_dir / "compare.png"))
    written = [images_dir / name for name in ("overlay.png", "recreated.png", "compare.png")]
    layout.append_log(panel_dir, {
        "step": "review", "agent_name": None, "agent_version": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "ok", "error": None,
        "files_written": [str(path.relative_to(panel_dir)) for path in written],
    })
    return written


def partition_stage_rows(
    candidates, finals, source_pdf="", review_html="", final_status=None
):
    """Return deduplicated accepted/review rows for one panel."""
    if final_status == "invalid_agent04":
        return {}, {}

    def exportable(row):
        explicit = str(row.get("export_eligible", "")).strip().lower()
        species = "".join(
            character
            for character in str(row.get("species", "")).upper()
            if character.isalnum()
        )
        return explicit == "true" and species == "CO2"

    candidate_by_id = {
        row.get("point_id"): row
        for row in candidates
        if row.get("point_id") and exportable(row)
    }
    final_by_id = {
        row.get("point_id"): row
        for row in finals
        if row.get("point_id") and exportable(row)
    }
    complete = final_status in (None, "complete_against_reviewed_evidence")
    # Once Agent 04 has a validated stage, its CSV is the entire inventory:
    # deleted candidate IDs must not reappear in review, and an intentionally
    # empty final CSV must remain empty. Candidates are provisional only when
    # no Agent 04 final exists at all.
    review_source = candidate_by_id if final_status == "missing_final" else final_by_id
    # A row placed where no marker centre was directly visible - a template fill or an
    # estimated point from ink/column/trajectory alone - always needs a human, regardless
    # of the panel's own verdict.
    accepted = {
        point_id: _enrich_stage_row(row, source_pdf, review_html)
        for point_id, row in (final_by_id.items() if complete else [])
        if str(row.get("is_inferred", "")).strip().lower() != "true"
    }
    review = {
        point_id: _enrich_stage_row(row, source_pdf, review_html)
        for point_id, row in review_source.items()
        if point_id not in accepted
    }
    return accepted, review


def stage_candidate_paths(out_dir, step="agent03"):
    """Yield every panel's <step>/points.csv under out_dir (src/models/layout.py layout)."""
    import os

    for root, dirs, files in os.walk(out_dir):
        dirs[:] = sorted(dirs)
        if pathlib.Path(root).name == step and "points.csv" in files:
            yield pathlib.Path(root) / "points.csv"


def write_stage_aggregations(out_dir):
    """Rebuild accepted/review/master CSVs exclusively from staged artifacts."""
    out_dir = pathlib.Path(out_dir)
    accepted_by_id = {}
    review_by_id = {}
    invalid = []
    panel_reports = []

    for candidate_path in sorted(stage_candidate_paths(out_dir)):
        panel_dir = candidate_path.parent.parent  # candidate_path is <panel>/agent03/points.csv
        try:
            candidates = _read_csv(candidate_path)
            audit_path = layout.step_file(panel_dir, "agent03", "audit.json")
            metadata = (
                (json.load(open(audit_path, encoding="utf-8")) or {}).get("metadata", {})
                if audit_path.exists()
                else {}
            )
            finals, final_source = _final_rows(panel_dir)
            final_audit_path = layout.step_file(panel_dir, "agent04", "audit.json")
            final_metadata = (
                (json.load(open(final_audit_path, encoding="utf-8")) or {}).get("metadata", {})
                if final_audit_path.exists()
                else {}
            )
            agent04_attempted = _agent04_attempted(panel_dir, final_metadata)
            if agent04_attempted and not finals:
                final_status = "invalid_agent04"
            else:
                final_status = final_metadata.get("status", "missing_final")
            source_pdf = str(metadata.get("source_pdf") or "")
            review_html = str((panel_dir / "review.html").resolve())
            human_review = _load_panel_review(out_dir, panel_dir)
            if human_review is not None:
                panel_key = str(panel_dir.relative_to(out_dir)).replace("\\", "/")
                candidates = overlay_reviewed_rows(candidates, human_review, panel_key)
                finals = overlay_reviewed_rows(finals, human_review, panel_key)
            panel_accepted, panel_review = partition_stage_rows(
                candidates,
                finals,
                source_pdf,
                review_html,
                final_status,
            )
            for point_id, row in panel_accepted.items():
                accepted_by_id.setdefault(point_id, row)
            for point_id, row in panel_review.items():
                review_by_id.setdefault(point_id, row)
            panel_reports.append(
                {
                    "panel_dir": str(panel_dir.resolve()),
                    "candidate_rows": len(candidates),
                    "accepted_rows": len(panel_accepted),
                    "review_rows": len(panel_review),
                    "human_review_applied": human_review is not None,
                    "status": final_status,
                    "final_source": final_source,
                    "final_invalid_reason": final_source if final_status == "invalid_agent04" else None,
                }
            )
        except Exception as error:
            invalid.append({"panel_dir": str(panel_dir.resolve()), "error": str(error)})

    # A globally accepted point wins over a stale review copy with the same ID.
    for point_id in accepted_by_id:
        review_by_id.pop(point_id, None)
    sort_key = lambda row: (
        row.get("source_pdf", ""),
        row.get("figure_id", ""),
        row.get("panel_id", ""),
        row.get("series_name", ""),
        row.get("point_id", ""),
    )
    accepted = sorted(accepted_by_id.values(), key=sort_key)
    review = sorted(review_by_id.values(), key=sort_key)
    all_rows = sorted([*accepted, *review], key=sort_key)

    master = out_dir / CFG["naming"]["master_table"]
    accepted_path = out_dir / "all_data_accepted.csv"
    review_path = out_dir / "all_data_review.csv"
    _write_rows(master, all_rows)
    _write_rows(accepted_path, accepted)
    _write_rows(review_path, review)
    report = {
        "schema_version": "4.0",
        "master_csv": str(master.resolve()),
        "accepted_csv": str(accepted_path.resolve()),
        "review_csv": str(review_path.resolve()),
        "index_html": str((out_dir / CFG["naming"]["dashboard"]).resolve()),
        "panel_count": len(panel_reports),
        "complete_panel_count": sum(
            panel["status"] == "complete_against_reviewed_evidence"
            for panel in panel_reports
        ),
        "accepted_row_count": len(accepted),
        "review_row_count": len(review),
        "row_count": len(all_rows),
        "missing_or_invalid_panels": invalid,
        "panels": panel_reports,
        "complete": not invalid
        and all(
            panel["status"] == "complete_against_reviewed_evidence"
            for panel in panel_reports
        ),
    }
    report_path = out_dir / "aggregation_report.json"
    tmp_report = pathlib.Path(str(report_path) + ".tmp")
    tmp_report.write_text(json.dumps(report, indent=1), encoding="utf-8")
    tmp_report.replace(report_path)
    return master, all_rows


def build_navigation(out_dir, rows):
    """Rebuild every review page and write the configured output index."""
    import os

    from src.tools import review_page

    pages = []
    for r in rows:  # one entry per panel, in table order
        if not pages or pages[-1]["review_html"] != r["review_html"]:
            pages.append(
                {
                    "review_html": r["review_html"],
                    "document": r["document"],
                    "figure_id": r["figure_id"],
                    "title": r["title"],
                    "document_title": r["document_title"],
                    "n": 0,
                    "flag": 0,
                    "series": set(),
                    "verdict_code": r["panel_verdict_code"],
                    "verdict_ai": r["panel_verdict_ai"],
                }
            )
        pages[-1]["n"] += 1
        pages[-1]["flag"] += bool(r["human_requires"])
        pages[-1]["series"].add(r["series_label"])
    index_path = out_dir / CFG["naming"]["dashboard"]
    for i, pg in enumerate(pages):
        here = pathlib.Path(pg["review_html"]).parent
        rel = lambda target: (
            os.path.relpath(target, here).replace(os.sep, "/") if target else None
        )
        label = lambda p: f"{p['document']} · {p['figure_id']}"
        nav = {
            "title": f"{pg['figure_id']} — {pg['document']}",
            "index_href": rel(index_path),
            "prev_href": rel(pages[i - 1]["review_html"]) if i > 0 else None,
            "next_href": rel(pages[i + 1]["review_html"])
            if i + 1 < len(pages)
            else None,
            "self_href": rel(pg["review_html"]),
            "options": [(rel(p["review_html"]), label(p)) for p in pages],
        }
        pdir = here
        if (pdir / "panel.png").exists():
            final_metadata = {}
            human_review = _load_panel_review(out_dir, pdir)
            final_audit_path = layout.step_file(pdir, "agent04", "audit.json")
            if final_audit_path.exists():
                final_metadata = (json.load(open(final_audit_path, encoding="utf-8")) or {}).get("metadata", {})
            result_path = None
            for candidate_path in (layout.step_file(pdir, "agent04", "points.json"),
                                   layout.step_file(pdir, "agent03", "points.json")):
                if not candidate_path.exists():
                    continue
                try:
                    candidate_result = json.load(open(candidate_path, encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                if any(
                    series.get("points")
                    for series in candidate_result.get("series", [])
                ):
                    result_path = candidate_path
                    break
            use_final = result_path is not None and result_path.parent.name == "agent04"
            if use_final and human_review is not None:
                # A saved review's points must be what the static comparison picture shows too,
                # not just the interactive plot: regenerate it from the reviewed points.
                regenerate_review_images(pdir)
            agent03_compare = layout.image_file(pdir, "agent03", "compare.png")
            agent04_compare = layout.image_file(pdir, "agent04", "compare.png")

            def _busted(path, href):
                return f"{href}?v={int(path.stat().st_mtime)}" if path.is_file() else href

            if result_path is not None:
                try:
                    review_page.build(
                        str(result_path),
                        str(pdir / "panel.png"),
                        pg["review_html"],
                        nav=nav,
                        panel_id=panel_id_of(pg["document"], pdir),
                        comparison_image=_busted(agent04_compare, "agent04/images/compare.png")
                        if use_final
                        else (
                            _busted(agent03_compare, "agent03/images/compare.png")
                            if agent03_compare.exists()
                            else _busted(agent04_compare, "agent04/images/compare.png")
                        ),
                        comparison_label="Agent 04 final reconstruction"
                        if use_final
                        else "Agent 03 candidate reconstruction",
                        additional_comparison_image=(
                            _busted(agent03_compare, "agent03/images/compare.png")
                            if use_final and agent03_compare.exists()
                            else None
                        ),
                        status=final_metadata.get("status"),
                        status_reason=(
                            "Agent 04 accepted the final extraction."
                            if use_final
                            else "The latest adjusted rows remain editable and require human review."
                        )
                        if final_metadata
                        else None,
                        human_review=human_review,
                    )
                except (OSError, ValueError, TypeError, KeyError) as error:
                    print(
                        f"== warning: could not rebuild {pg['review_html']}: {error}",
                        file=sys.stderr,
                    )
    write_dashboard(out_dir, pages, rows)


def write_dashboard(out_dir, pages, rows):
    """Build the self-contained dashboard with KPIs, filters, and overlay thumbnails."""
    import html
    import os

    docs = {}
    for pg in pages:
        docs.setdefault(
            pg["document"],
            {"title": pg["document_title"], "panels": 0, "points": 0, "flag": 0},
        )
        d = docs[pg["document"]]
        d["panels"] += 1
        d["points"] += pg["n"]
        d["flag"] += pg["flag"]
    series_colors = {}
    for r in rows:
        series_colors.setdefault(
            (r["review_html"], r["series_label"]),
            r["context_color"].split()[-1] if r["context_color"] else "#888",
        )
    cards = []
    for pg in pages:
        here = pathlib.Path(pg["review_html"]).parent
        href = os.path.relpath(pg["review_html"], out_dir).replace(os.sep, "/")
        overlay = layout.image_file(here, "agent04", "overlay.png")
        if not overlay.exists():
            overlay = layout.image_file(here, "agent03", "overlay.png")
        thumb = os.path.relpath(overlay, out_dir).replace(os.sep, "/")
        status = (
            "needs_human"
            if pg["flag"]
            else (
                "accepted"
                if pg["verdict_code"] == "auto_accept"
                and pg["verdict_ai"] in ("accept", "", None)
                else "review"
            )
        )
        cards.append(
            {
                "document": pg["document"],
                "document_title": pg["document_title"],
                "figure_id": pg["figure_id"],
                "title": pg["title"],
                "series": [
                    {
                        "label": s,
                        "color": series_colors.get((pg["review_html"], s), "#888"),
                    }
                    for s in sorted(pg["series"])
                ],
                "points": pg["n"],
                "flag": pg["flag"],
                "checks": pg["verdict_code"],
                "ai": pg["verdict_ai"] or "",
                "href": href,
                "thumb": thumb,
                "status": status,
            }
        )
    n_points = sum(p["n"] for p in pages)
    n_flag = sum(p["flag"] for p in pages)
    n_ok = sum(1 for c in cards if c["status"] == "accepted")
    kpis = [
        ("documents", len(docs)),
        ("figure panels", len(pages)),
        ("data points", n_points),
        ("points needing a human", n_flag),
        ("panels auto-accepted", f"{n_ok}/{len(pages)}"),
    ]
    doc_rows = "".join(
        f'<tr><td><b>{html.escape(k)}</b><div class="muted">{html.escape(v["title"] or "")}</div></td>'
        f'<td>{v["panels"]}</td><td>{v["points"]}</td><td class="{"bad" if v["flag"] else "good"}">{v["flag"]}</td></tr>'
        for k, v in docs.items()
    )
    page = TEMPLATE.replace(
        "__STYLESHEET__", STYLESHEET_PATH.read_text(encoding="utf-8")
    )
    page = page.replace(
        "__KPIS__",
        "".join(
            f'<div class="kpi"><div class="v">{v}</div><div class="k">{k}</div></div>'
            for k, v in kpis
        ),
    )
    page = (
        page.replace("__DOCS__", doc_rows)
        .replace("__CARDS__", json.dumps(cards))
        .replace("__CSV__", CFG["naming"]["master_table"])
    )
    page = page.replace("__STAMP__", __import__("time").strftime("%Y-%m-%d %H:%M"))
    (out_dir / CFG["naming"]["dashboard"]).write_text(page, encoding="utf-8")


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CO2 isotherm workflow</title>
<style>
__STYLESHEET__

.header-link { color: #ffffff; }
.card-actions { display: grid; gap: 8px; grid-template-columns: 1fr 1fr; }
.btn.secondary { background: var(--card); color: var(--primary-dark); }
.btn.secondary:hover { background: var(--primary-light); border-color: var(--primary-dark); }
@media (max-width: 700px) { .card-actions { grid-template-columns: 1fr; } }
</style></head><body>
<header><h1>CO2 isotherm workflow</h1><p>Every extracted chart, one click from its review page. Master table: <a class="header-link" href="__CSV__">__CSV__</a></p></header>
<main>
<div class="kpis">__KPIS__</div>
<h2>Documents</h2>
<table><tr><th>document</th><th>panels</th><th>points</th><th>needs human</th></tr>__DOCS__</table>
<h2>Figures</h2>
<div class="bar">
  <span class="chip on" data-f="all">All</span><span class="chip" data-f="needs_human">Needs a human</span><span class="chip" data-f="accepted">Accepted</span><span class="chip" data-f="review">Review</span>
  <input type="search" id="q" placeholder="search document, figure id, title, series…">
  <select id="sort"><option value="doc">sort: document · figure</option><option value="flag">sort: needs human first</option><option value="points">sort: most points</option></select>
</div>
<div class="grid" id="grid"></div><div class="empty" id="empty" hidden>Nothing matches.</div>
<footer>Built __STAMP__ · direct save and automatic rebuild: <code>python scripts/review_server.py</code></footer>
</main>
<script>
const cards=__CARDS__;let filter="all",q="",sort="doc";
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function render(){
  let list=cards.filter(c=>filter==="all"||c.status===filter);
  if(q){const t=q.toLowerCase();list=list.filter(c=>[c.document,c.document_title,c.figure_id,c.title,...c.series.map(s=>s.label)].join(" ").toLowerCase().includes(t));}
  list.sort((a,b)=>sort==="flag"?(b.flag-a.flag)||a.document.localeCompare(b.document):sort==="points"?b.points-a.points:a.document.localeCompare(b.document)||a.figure_id.localeCompare(b.figure_id));
  document.getElementById("grid").innerHTML=list.map(c=>`<div class="card">
    <a href="${c.href}"><img class="thumb" loading="lazy" src="${c.thumb}" alt=""></a>
    <div class="body"><div class="top"><span class="fid">${esc(c.figure_id)}</span><span class="badge ${c.status}">${c.status.replace("_"," ")}</span></div>
    <div class="title">${esc(c.title)}</div><div class="doc">${esc(c.document_title||c.document)}</div>
    <div class="series">${c.series.map(s=>`<span class="s"><i class="dot" style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>
    <div class="meta"><span><b>${c.points}</b> points</span><span><b class="${c.flag?"bad":"good"}">${c.flag}</b> need a human</span><span>checks <b>${esc(c.checks)}</b></span><span>AI <b>${esc(c.ai||"–")}</b></span></div>
    <div class="card-actions"><a class="btn" href="${c.href}">Open review page →</a><a class="btn secondary" href="${c.href.replace('review.html','agent04/images/compare.png')}">Original vs recreated</a></div></div></div>`).join("");
  document.getElementById("empty").hidden=list.length>0;
}
document.querySelectorAll(".chip").forEach(ch=>ch.onclick=()=>{document.querySelectorAll(".chip").forEach(x=>x.classList.remove("on"));ch.classList.add("on");filter=ch.dataset.f;render();});
document.getElementById("q").oninput=e=>{q=e.target.value;render();};
document.getElementById("sort").onchange=e=>{sort=e.target.value;render();};
render();
</script></body></html>"""


if __name__ == "__main__":
    out, n = write_master_table(sys.argv[1] if len(sys.argv) > 1 else "out")
    print(f"{n} points -> {out}")
