"""Unstructured Code Interpreter responses with validated artifact reports.

Stage-specific YAML profiles select individual input files. Each attempt downloads cited files
straight into the panel's step folder (src/models/layout.py) and appends one entry to log.json.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import pathlib
import re
import uuid

from src.models import layout

log = logging.getLogger("chart_extract")


def _citations(value):
    if isinstance(value, dict):
        if value.get("type") == "container_file_citation":
            yield value
        for child in value.values():
            yield from _citations(child)
    elif isinstance(value, list):
        for child in value:
            yield from _citations(child)


def _files_sent(paths):
    """Logical source paths logged for uploaded files and inline images."""
    return [
        getattr(path, "logical_path", pathlib.Path(getattr(path, "path", path)).name)
        for path in paths
    ]


async def _with_progress(call, name, timeout):
    task = asyncio.create_task(call)
    started = asyncio.get_running_loop().time()
    try:
        while True:
            elapsed = asyncio.get_running_loop().time() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                raise TimeoutError(f"{name}: file review exceeded {timeout}s")
            done, _ = await asyncio.wait({task}, timeout=min(30, remaining))
            if done:
                return await task
            log.info("%s: file review still running (%ds elapsed)", name,
                     round(asyncio.get_running_loop().time() - started))
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def download_cited_files(client, payload, directory, timeout=180):
    """Download all completed-response citations without prescribed output names."""
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        raise RuntimeError(f"Artifact response did not complete: {payload.get('status') if isinstance(payload, dict) else 'unknown'}")
    citations = []
    seen_files = set()
    used_names = set()
    versions = {}
    for citation in _citations(payload):
        name = pathlib.PurePosixPath(str(citation.get("filename", "")).replace("\\", "/")).name
        if not name or name in {".", ".."}:
            continue
        if not citation.get("file_id") or not citation.get("container_id"):
            raise RuntimeError(f"Incomplete container citation: {name}")
        identity = (name, citation["file_id"], citation["container_id"])
        if identity in seen_files:
            continue
        seen_files.add(identity)
        version = versions.get(name, 0)
        while True:
            version += 1
            suffix = pathlib.Path(name).suffix
            stem = name[:-len(suffix)] if suffix else name
            local_name = name if version == 1 else f"{stem}__citation_{version}{suffix}"
            if local_name not in used_names:
                break
        versions[name] = version
        used_names.add(local_name)
        citations.append((local_name, citation))
    downloaded = {}
    for name, citation in citations:
        content = await client.containers.files.content.retrieve(
            file_id=citation["file_id"], container_id=citation["container_id"],
            timeout=timeout,
        )
        data = content.read()
        if inspect.isawaitable(data):
            data = await data
        if not data:
            raise RuntimeError(f"Downloaded artifact is empty: {name}")
        path = pathlib.Path(directory) / name
        path.write_bytes(data)
        downloaded[name] = path
    return downloaded


def _json_objects(text):
    """Yield object-shaped JSON from bare, fenced, or prose-wrapped output."""
    if not isinstance(text, str):
        return
    decoder = json.JSONDecoder()
    bodies = [text.strip()]
    bodies.extend(match.group(1).strip() for match in re.finditer(
        r"```(?:json)?\s*\n?(.*?)```", text, flags=re.IGNORECASE | re.DOTALL,
    ))
    for body in bodies:
        for start in (0, *(index for index, char in enumerate(body) if char == "{")):
            try:
                value, _ = decoder.raw_decode(body[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _response_text(payload):
    """Return all assistant text from a Responses payload for failure evidence."""
    return "\n\n".join(
        part["text"]
        for item in payload.get("output", []) if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text" and isinstance(part.get("text"), str)
    ) if isinstance(payload, dict) else ""


def _report_from_artifacts(artifacts, report_name, validator, payload):
    """Find a schema-valid report by content, including inline JSON output."""
    candidates = []
    for name, path in artifacts.items():
        if pathlib.Path(name).suffix.lower() != ".json":
            continue
        try:
            reports = _json_objects(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError):
            continue
        for report in reports:
            if not list(validator.iter_errors(report)):
                candidates.append((name, report))
    if candidates:
        # A response may cite earlier drafts and the final rewrite under the same
        # basename. Citation order is the only transport-level version signal.
        return candidates[-1][1]
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") != "output_text":
                continue
            for report in _json_objects(part.get("text", "")):
                if not list(validator.iter_errors(report)):
                    return report
    raise ValueError("Artifact response contains no schema-valid JSON report")


async def ask_artifacts(agent, values, *, files, images=(), panel_dir, step,
                        report_name, dry_run_files=None, artifact_subdir=None,
                        download_citations=True):
    """One Code Interpreter attempt using the individual files selected for its stage."""
    from src.workflow.agents import _image_input, _jsonable, _provenance, _next_attempt, dry_run

    if not report_name or report_name in {".", ".."} or "/" in report_name or "\\" in report_name:
        raise ValueError("Artifact report name must be a basename")
    files, images = tuple(files), tuple(map(pathlib.Path, images))
    upload_names = [
        str(getattr(item, "upload_name", pathlib.Path(getattr(item, "path", item)).name))
        for item in files
    ]
    if len(set(upload_names)) != len(upload_names):
        raise ValueError("Uploaded basenames must be unique")
    step_dir = layout.step_dir(panel_dir, step)
    artifact_dir = step_dir / artifact_subdir if artifact_subdir else step_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    text = agent.prompt(values)
    entry = {
        "step": step, "agent_name": None, "agent_version": None, "model": None, "created_at": None,
        "response_id": None, "usage": None, "attempt": _next_attempt(panel_dir, step),
        "status": "failed", "error": None,
        "files_sent": _files_sent((*files, *images)),
        "uploaded_names": upload_names,
        "dry_run": dry_run(),
    }
    uploaded = []
    client = None
    project = None
    runtime_name = None
    artifacts = {}
    payload = {"output": []}

    async def invoke():
        nonlocal client, project, runtime_name, artifacts, payload
        from azure.ai.projects.models import PromptAgentDefinition

        client = agent._get_client()
        project = agent._get_project_client()
        base = await project.agents.get_version(agent_name=agent.agent_name, agent_version=agent.agent_version)
        definition = copy.deepcopy(base.definition.as_dict() if hasattr(base.definition, "as_dict") else dict(base.definition))
        if definition.get("kind") != "prompt":
            raise RuntimeError("The pinned reviewer must be a Prompt Agent before rerunning")
        definition.pop("reasoning", None)
        code_tools = [tool for tool in definition.get("tools", []) if tool.get("type") == "code_interpreter"]
        if len(code_tools) != 1:
            raise RuntimeError("The pinned reviewer must have one Code Interpreter tool before rerunning")
        if agent.config.instructions.strip():
            definition["instructions"] = agent.config.instructions.strip()
        for item, upload_name in zip(files, upload_names):
            path = pathlib.Path(getattr(item, "path", item))
            with path.open("rb") as handle:
                upload = handle if upload_name == path.name else (upload_name, handle)
                result = await client.files.create(file=upload, purpose="assistants",
                                                   timeout=agent.config.timeout_seconds)
            uploaded.append(result.id)
        code_tools[0]["container"] = {"type": "auto", "file_ids": uploaded, "memory_limit": "4g"}
        runtime_name = f"chart-review-{uuid.uuid4().hex[:20]}"
        runtime = await project.agents.create_version(
            agent_name=runtime_name, definition=PromptAgentDefinition(definition),
            description="Temporary chart review with this panel's runtime inputs",
            metadata={"source_agent": agent.agent_name, "source_version": agent.agent_version},
        )
        log.info("%s: invoking Prompt Agent %s version %s (Code Interpreter, complete panel)",
                 agent.config.display_name, agent.agent_name, agent.agent_version)
        response = await client.responses.create(
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": text},
                *(_image_input(path) for path in images),
            ]}],
            extra_body={"agent_reference": {
                "name": runtime_name, "version": str(runtime.version),
                "type": "agent_reference",
            }},
            timeout=agent.config.timeout_seconds,
        )
        payload = _jsonable(response)
        agent.last_provenance = _provenance(response, agent.agent_name, agent.agent_version)
        entry.update(agent_name=agent.last_provenance["agent_name"], agent_version=agent.last_provenance["agent_version"],
                     model=agent.last_provenance.get("model"), created_at=agent.last_provenance["created_at"],
                     response_id=agent.last_provenance.get("response_id"),
                     usage=agent.last_provenance.get("usage"))
        # Agent 04 returns its complete table inline.  Do not touch the
        # container-files endpoint for that path: it is neither required for
        # the response contract nor reliably available with Foundry Entra auth.
        if download_citations:
            artifacts = await download_cited_files(client, payload, artifact_dir, agent.config.timeout_seconds)

    try:
        if dry_run():
            if dry_run_files is None or report_name not in dry_run_files:
                raise ValueError("Dry artifact run needs a report fixture")
            for name, data in dry_run_files.items():
                if not name or name in {".", ".."} or "/" in name or "\\" in name:
                    raise ValueError("Dry artifact names must be basenames")
                path = step_dir / name
                path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
                artifacts[name] = path
            agent.last_provenance = {"agent_name": agent.agent_name, "agent_version": agent.agent_version,
                                     "model": None,
                                     "response_mode": "artifacts", "prompt": None, "dry_run": True,
                                     "response_id": None, "usage": {}}
            entry.update(agent_name=agent.agent_name, agent_version=agent.agent_version)
        else:
            await _with_progress(invoke(), agent.config.display_name, agent.config.timeout_seconds)
        report = _report_from_artifacts(artifacts, report_name, agent.validator, payload)
        report_path = step_dir / report_name
        report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        artifacts[report_name] = report_path
        entry["status"] = "ok"
        return {"report": report, "artifacts": artifacts,
                "provenance": agent.last_provenance}
    except BaseException as error:
        entry["error"] = str(error)
        transcript = _response_text(payload)
        if transcript:
            (artifact_dir / "response.txt").write_text(transcript, encoding="utf-8")
        raise
    finally:
        layout.append_log(panel_dir, entry)
        if runtime_name is not None:
            try:
                await asyncio.wait_for(project.agents.delete(agent_name=runtime_name), timeout=10)
            except Exception:
                log.warning("%s: could not remove temporary runtime agent %s", agent.config.display_name, runtime_name)
        # Uploaded inputs are temporary Foundry-side files. Local source files belong to the run.
        for file_id in uploaded:
            try:
                await asyncio.wait_for(client.files.delete(file_id), timeout=5)
            except Exception:
                log.warning("%s: could not remove temporary uploaded input %s", agent.config.display_name, file_id)
