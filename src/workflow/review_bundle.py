"""Pack the complete panel evidence into a Code Interpreter input archive.

The archive is written to a temp file (tempfile) - the caller (src/workflow/artifact_agent.py)
uploads it and deletes it when the call finishes; nothing is kept on disk.
"""

import io
import csv
import hashlib
import json
import pathlib
import tempfile
from datetime import datetime, timezone
import zipfile

from src.models import layout
from src.settings import ROOT
from src.tools.stage_artifacts import STAGE_COLUMNS


def csv_text(rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=STAGE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _file_fingerprint(path, label):
    path = pathlib.Path(path)
    if not path.is_file():
        return {"path": label, "missing": True}
    return {"path": label, "sha256": _sha256_bytes(path.read_bytes())}


def build_bundle(panel_dir, stage, *, schema, candidate=None, rows=None,
                 prior_report=None):
    """Package one canonical candidate plus unique native review evidence.

    Agent 03 receives the recovered review candidate created for that call.
    Agent 04 receives the completed Agent 03 candidate. Older Python tables,
    round snapshots, redraws, and audit reports are deliberately excluded:
    they repeat the candidate or bias review toward historical proposals.
    """
    panel_dir = pathlib.Path(panel_dir)
    if stage not in {"agent03", "agent04"}:
        raise ValueError(f"unsupported review bundle stage: {stage}")
    names = ["panel.png", "spec.json", "python/supporting_traces.json"]
    required = ["panel.png", "spec.json"]
    if stage == "agent03":
        if candidate is None or rows is None:
            raise ValueError("Agent 03 bundle requires its canonical review candidate and rows")
        names.append("agent03/measurement_targets.json")
    else:
        names += ["agent03/points.csv", "agent03/points.json", "agent03/answer.json"]
        required += ["agent03/points.csv", "agent03/points.json", "agent03/answer.json"]
    for name in required:
        if not (panel_dir / name).is_file():
            raise FileNotFoundError(f"Missing file review input: {name}")
    handle = tempfile.NamedTemporaryFile(prefix=f"{stage}-inputs-", suffix=".zip", delete=False)
    handle.close()
    path = pathlib.Path(handle.name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            file = panel_dir / name
            if file.is_file():
                archive.write(file, name)
        tiles = layout.images_dir(panel_dir, "agent03") / "tiles"
        if tiles.is_dir():
            for file in sorted(tiles.rglob("*")):
                if file.is_file() and file.suffix.lower() in {".png", ".json"}:
                    # Do not follow a link into another panel or outside the workspace.
                    file.resolve().relative_to(panel_dir.resolve())
                    archive.write(file, file.relative_to(panel_dir).as_posix())
        archive.writestr("response.schema.json", json.dumps(schema, indent=2))
        archive.write(ROOT / "schemas/shared.schema.json", "shared.schema.json")
        archive.write(ROOT / "schemas/points_csv.contract.json", "points_csv.contract.json")
        archive.write(ROOT / "examples/points.example.csv", "points.example.csv")
        if stage == "agent03":
            archive.writestr("review_points.json", json.dumps(candidate, indent=1))
            archive.writestr("review_points.csv", csv_text(rows))
            if prior_report is not None:
                archive.writestr("prior_agent03_report.json", json.dumps(prior_report, indent=1))
    # Reopen the completed archive before reading its members. On Windows,
    # ZipInfo.from_file retains backslashes in orig_filename until it is read
    # back from disk, while the written member header uses forward slashes.
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        # A compact receipt makes the actual review input reproducible without
        # copying credentials, the whole repository, or generated source data.
        archive_files = [
            {"path": info.filename, "sha256": _sha256_bytes(archive.read(info.filename))}
            for info in archive.infolist()
        ]
        answer_path = panel_dir / "agent02" / "answer.json"
        try:
            answer = json.loads(answer_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            answer = {}
        prompt_name = "agent03_review_points.md" if stage == "agent03" else "agent04_final_check.md"
        schema_name = "agent03_review_points.schema.json" if stage == "agent03" else "agent04_final_check.schema.json"
        selected_round = answer.get("selected_round") or {}
        application = answer.get("application") or {}
        chosen_identity = (selected_round.get("selected") if isinstance(selected_round, dict) else None) or (
            application.get("round") if isinstance(application, dict) else None
        ) or answer.get("round") or answer.get("check_round") or answer.get("round_id") or "latest_checked"
        input_manifest = {
            "schema_version": "1.0",
            "role": "review_input_receipt",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "canonical_candidate": (
                "review_points.csv + review_points.json" if stage == "agent03"
                else "agent03/points.csv + agent03/points.json"
            ),
            "archive_files": archive_files,
            "fingerprints": [
                _file_fingerprint(ROOT / "prompts" / prompt_name, f"prompts/{prompt_name}"),
                _file_fingerprint(ROOT / "schemas" / schema_name, f"schemas/{schema_name}"),
                _file_fingerprint(ROOT / "schemas" / "points_csv.contract.json", "schemas/points_csv.contract.json"),
                _file_fingerprint(ROOT / "schemas" / "shared.schema.json", "schemas/shared.schema.json"),
                _file_fingerprint(ROOT / "workflow.yaml", "workflow.yaml"),
            ],
            "chosen_round": {
                "identity": chosen_identity,
                "source": "agent02/answer.json",
                "policy": "current checked proposal is review input; source pixels are authoritative and historical scores are diagnostic only",
            },
        }
        archive.writestr("input_manifest.json", json.dumps(input_manifest, indent=1))
    receipt = layout.step_file(panel_dir, stage, f"bundle_receipt_{path.stem}.json")
    receipt.write_text(json.dumps(input_manifest, indent=1), encoding="utf-8")
    return path
