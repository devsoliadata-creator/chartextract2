"""Panel folder layout: one place that knows where every artifact lives.

Every writer and reader in src/ and scripts/ builds panel-relative paths through these
functions instead of hand-rolling names like ``f"{stage}_points.csv"``.  See docs/files.md
for the resulting tree.
"""

from __future__ import annotations

import json
import pathlib

STEPS = ("python", "agent01", "agent02", "agent03", "agent04")


def step_dir(panel_dir, step) -> pathlib.Path:
    """<panel>/<step>/, created on demand."""
    if step not in STEPS:
        raise ValueError(f"unknown step {step!r}; steps are {STEPS}")
    path = pathlib.Path(panel_dir) / step
    path.mkdir(parents=True, exist_ok=True)
    return path


def images_dir(panel_dir, step) -> pathlib.Path:
    """<panel>/<step>/images/, created on demand."""
    path = step_dir(panel_dir, step) / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def step_file(panel_dir, step, name) -> pathlib.Path:
    """<panel>/<step>/<name>."""
    return step_dir(panel_dir, step) / name


def image_file(panel_dir, step, name) -> pathlib.Path:
    """<panel>/<step>/images/<name>."""
    return images_dir(panel_dir, step) / name


def round_file(panel_dir, n) -> pathlib.Path:
    """<panel>/agent02/round<n>.json."""
    return step_file(panel_dir, "agent02", f"round{int(n)}.json")


def python_round_dir(panel_dir, n) -> pathlib.Path:
    """<panel>/python/rounds/round<n>/, created on demand - a snapshot of one Python extraction round."""
    path = step_dir(panel_dir, "python") / "rounds" / f"round{int(n)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def batch_file(panel_dir, n) -> pathlib.Path:
    """<panel>/agent03/batches/batch_<nnn>.json."""
    path = step_dir(panel_dir, "agent03") / "batches"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"batch_{int(n):03d}.json"


def log_file(panel_dir) -> pathlib.Path:
    """<panel>/log.json - one timeline of every model call / attempt for the panel."""
    return pathlib.Path(panel_dir) / "log.json"


def append_log(panel_dir, entry: dict) -> None:
    """Append one entry to the panel's log.json, read-modify-write, atomic replace."""
    path = log_file(panel_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {"entries": []}
    payload.setdefault("entries", []).append(entry)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)
