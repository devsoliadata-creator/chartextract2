"""Resolve editable, stage-specific Code Interpreter inputs from runtime/*.yaml."""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import pathlib
from dataclasses import dataclass

import yaml

from src.models import layout
from src.settings import ROOT


@dataclass(frozen=True)
class RuntimeInput:
    """One local file and the basename it will have in the Code Interpreter container."""

    path: pathlib.Path
    upload_name: str
    logical_path: str


def csv_text(rows):
    """Serialize candidate rows with the canonical extraction CSV contract."""
    from src.tools.stage_artifacts import STAGE_COLUMNS

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=STAGE_COLUMNS, extrasaction="ignore", lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _root_path(name: str, panel_dir: pathlib.Path | None) -> pathlib.Path:
    if name == "panel":
        if panel_dir is None:
            raise ValueError("panel runtime inputs require a panel directory")
        return panel_dir.resolve()
    if name == "project":
        return ROOT.resolve()
    raise ValueError(f"unknown runtime input root: {name!r}")


def _safe_relative_path(value: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"runtime input path must be a safe relative path: {value!r}")
    return path


def _logical_path(root_name: str, path: pathlib.Path, root: pathlib.Path) -> str:
    relative = path.relative_to(root).as_posix()
    return relative if root_name == "panel" else f"project/{relative}"


def resolve_runtime_inputs(stage: str, panel_dir=None, *, conditions=(), sources=None) -> list[RuntimeInput]:
    """Read one stage YAML profile and return its existing, ordered files."""
    if stage not in {"agent00", "agent01", "agent02", "agent03", "agent04"}:
        raise ValueError(f"unsupported Code Interpreter stage: {stage!r}")
    panel_dir = pathlib.Path(panel_dir).resolve() if panel_dir is not None else None
    sources = {str(key): pathlib.Path(value).resolve() for key, value in (sources or {}).items()
               if value is not None}
    profile_path = ROOT / "runtime" / f"{stage}.yaml"
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    if profile.get("version") != 1 or not isinstance(profile.get("files"), list):
        raise ValueError(f"invalid runtime profile: {profile_path}")

    enabled = set(conditions)
    resolved: list[RuntimeInput] = []
    names = set()
    for item in profile["files"]:
        condition = item.get("when")
        if condition and condition not in enabled:
            continue
        if "source" in item:
            source_name = str(item["source"])
            source = sources.get(source_name)
            candidates = [source] if source is not None and source.is_file() else []
            root_name, root = f"source:{source_name}", None
            if not candidates and item.get("required", True):
                raise FileNotFoundError(f"required {stage} runtime source is missing: {source_name}")
        else:
            root_name = item.get("root", "panel")
            root = _root_path(root_name, panel_dir)
            if "path" in item:
                relative = _safe_relative_path(item["path"])
                candidate = (root / pathlib.Path(*relative.parts)).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError as error:
                    raise ValueError(f"runtime input escapes its {root_name} root: {item['path']!r}") from error
                candidates = [candidate] if candidate.is_file() else []
                if not candidates and item.get("required", True):
                    raise FileNotFoundError(f"required {stage} runtime input is missing: {item['path']}")
            elif "glob" in item:
                pattern = _safe_relative_path(item["glob"]).as_posix()
                candidates = sorted(path.resolve() for path in root.glob(pattern) if path.is_file())
                exclusions = item.get("exclude_names") or []
                candidates = [
                    candidate for candidate in candidates
                    if not any(fnmatch.fnmatchcase(candidate.name, mask) for mask in exclusions)
                ]
                for candidate in candidates:
                    try:
                        candidate.relative_to(root)
                    except ValueError as error:
                        raise ValueError(f"runtime glob escaped its {root_name} root: {item['glob']!r}") from error
                if not candidates and item.get("required", False):
                    raise FileNotFoundError(f"required {stage} runtime input glob matched no files: {item['glob']}")
            else:
                raise ValueError(f"runtime input needs source, path or glob: {item!r}")

        for candidate in candidates:
            upload_name = str(item.get("upload_as") or candidate.name)
            if pathlib.PurePath(upload_name).name != upload_name or upload_name in {"", ".", ".."}:
                raise ValueError(f"runtime upload name must be a basename: {upload_name!r}")
            if upload_name in names:
                raise ValueError(f"duplicate {stage} runtime upload name: {upload_name}")
            names.add(upload_name)
            resolved.append(RuntimeInput(
                path=candidate,
                upload_name=upload_name,
                logical_path=(f"{root_name}/{candidate.name}" if root is None
                              else _logical_path(root_name, candidate, root)),
            ))
    return resolved


def describe_runtime_inputs(inputs: list[RuntimeInput]) -> str:
    """Describe exact container filenames and their project/panel source paths for the prompt."""
    return "\n".join(
        f"- `/mnt/data/{item.upload_name}` (source: `{item.logical_path}`)"
        for item in inputs
    ) or "- no files configured"


def prepare_runtime_inputs(stage: str, panel_dir, *, schema, candidate_rows=None,
                           prior_report=None) -> list[RuntimeInput]:
    """Persist the small generated stage files, then resolve the stage's editable profile."""
    if stage not in {"agent03", "agent04"}:
        raise ValueError(f"unsupported Code Interpreter stage: {stage!r}")
    panel_dir = pathlib.Path(panel_dir)
    stage_dir = layout.step_dir(panel_dir, stage)
    schema_path = stage_dir / "response.schema.json"
    schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")

    conditions = set()
    if stage == "agent03":
        if candidate_rows is None:
            raise ValueError("Agent 03 runtime inputs require its candidate rows")
        (stage_dir / "review_points.csv").write_text(csv_text(candidate_rows), encoding="utf-8")
        if prior_report is not None:
            prior_path = stage_dir / "prior_agent03_report.json"
            prior_path.write_text(json.dumps(prior_report, indent=1, ensure_ascii=False), encoding="utf-8")
            conditions.add("prior_report")
    return resolve_runtime_inputs(stage, panel_dir, conditions=conditions)
