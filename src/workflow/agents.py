"""The one place that talks to Foundry Prompt Agents.

Each agent step in workflow.yaml references a pinned Foundry Prompt Agent. A call sends one
local prompt (rendered from prompts/<step>.md), the step's ordered images, and local schema
instructions; the answer is validated against the same schema before
the workflow uses it.

Dry run: set ``CHART_EXTRACT_DRY_RUN=1`` to answer every call with the step's example
(examples/<step>.example.json) instead of calling a model.  Use it to wire up and preview
the workflow without Azure access or cost; the extracted data is then meaningless.
"""

from __future__ import annotations

import base64
import copy
import datetime
import json
import logging
import os
import pathlib

import cv2
from jsonschema import Draft202012Validator

from src.models import layout
from src.workflow.resources import AgentConfig, load_json, load_schema, load_workflow, render_prompt, vocabulary

log = logging.getLogger("chart_extract")

DRY_RUN_ENV = "CHART_EXTRACT_DRY_RUN"
AUTH_MODE_ENV = "FOUNDRY_AUTH_MODE"
PROJECT_API_KEY_ENV = "FOUNDRY_PROJECT_API_KEY"
OPENAI_MAX_RETRIES = 0

_clients: dict[tuple[str, str], object] = {}
_projects: dict[str, object] = {}
_credentials: list[object] = []
# A Prompt Agent call can be long-running.  Retrying inside the SDK is unsafe
# during Ctrl-C shutdown on Windows: the interrupted transport may otherwise
# start another paid request while asyncio is closing the event loop.  Surface
# transient failures to the workflow/user instead of retrying invisibly.

def dry_run() -> bool:
    return os.environ.get(DRY_RUN_ENV, "").strip().lower() in ("1", "true", "yes")


def _auth_mode() -> str:
    """Return the configured Foundry authentication mode."""
    mode = os.environ.get(AUTH_MODE_ENV, "entra").strip().lower().replace("-", "_")
    aliases = {"azure_ad": "entra", "entra_id": "entra", "key": "api_key"}
    mode = aliases.get(mode, mode)
    if mode not in {"entra", "api_key"}:
        raise RuntimeError(
            f"unsupported {AUTH_MODE_ENV}={mode!r}; use 'entra' or 'api_key'"
        )
    return mode


def _project_client():
    """Project client for pinned-version checks and temporary file-review agents."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("set FOUNDRY_PROJECT_ENDPOINT to the Foundry project endpoint")
    if endpoint not in _projects:
        from azure.ai.projects.aio import AIProjectClient
        from azure.identity.aio import DefaultAzureCredential

        credential = DefaultAzureCredential()
        _credentials.append(credential)
        _projects[endpoint] = AIProjectClient(endpoint=endpoint, credential=credential, retry_total=0)
    return _projects[endpoint]


async def close_clients():
    """Close HTTP sessions and credentials before the CLI event loop exits."""
    resources = [*_clients.values(), *_projects.values(), *_credentials]
    _clients.clear()
    _projects.clear()
    _credentials.clear()
    seen = set()
    for resource in resources:
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        try:
            await resource.close()
        except Exception as error:
            log.warning("Could not close Foundry client: %s", error)


async def with_client_cleanup(call):
    try:
        return await call
    finally:
        await close_clients()


def _openai_client():
    """Return the project OpenAI client used to invoke Prompt Agents."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("set FOUNDRY_PROJECT_ENDPOINT to the Foundry project endpoint")
    mode = _auth_mode()
    cache_key = (mode, endpoint)
    if cache_key in _clients:
        return _clients[cache_key]

    if mode == "entra":
        client = _project_client().get_openai_client(max_retries=OPENAI_MAX_RETRIES)
    else:
        from openai import AsyncOpenAI

        api_key = os.environ.get(PROJECT_API_KEY_ENV) or os.environ.get("FOUNDRY_API_KEY")
        if not api_key:
            raise RuntimeError(
                f"{AUTH_MODE_ENV}=api_key requires {PROJECT_API_KEY_ENV} or FOUNDRY_API_KEY"
            )
        client = AsyncOpenAI(
            base_url=f"{endpoint}/openai/v1",
            api_key=api_key,
            max_retries=OPENAI_MAX_RETRIES,
        )

    _clients[cache_key] = client
    return client


def _image_input(path) -> dict:
    """Return an ordered Responses API image item, resized to the workflow limit."""
    image = cv2.imread(str(path))
    if image is None:
        raise FileNotFoundError(f"cannot read image {path}")
    limit = load_workflow().max_image_side
    height, width = image.shape[:2]
    if max(height, width) > limit:
        scale = limit / max(height, width)
        image = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    ok, png = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"cannot encode image {path}")
    encoded = base64.b64encode(png.tobytes()).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{encoded}",
    }


def _jsonable(value):
    """Convert SDK response metadata into JSON-safe values for provenance files."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    for method in ("model_dump", "to_dict"):
        converter = getattr(value, method, None)
        if callable(converter):
            return _jsonable(converter())
    return repr(value)


def _response_text(response) -> str:
    """Return text output or raise a useful error for refusal/filter/incomplete output."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    details = []
    for attribute in ("status", "incomplete_details", "error"):
        value = getattr(response, attribute, None)
        if value is not None:
            details.append(f"{attribute}={_jsonable(value)!r}")
    for item in getattr(response, "output", None) or []:
        item_type = str(getattr(item, "type", ""))
        if "refusal" in item_type.lower():
            details.append(f"refusal={_jsonable(getattr(item, 'content', item))!r}")
    suffix = "; ".join(details) or "no response details"
    raise RuntimeError(f"Prompt Agent returned no text output ({suffix})")


def _response_model(response):
    """Return the model identifier reported by the Responses SDK.

    Prompt Agent calls do not pass ``model=`` in the request; the pinned agent
    selects it server-side.  The completed response is therefore the source of
    truth for telemetry and pricing.
    """
    model = getattr(response, "model", None)
    if model:
        return str(model)
    if isinstance(response, dict) and response.get("model"):
        return str(response["model"])
    return None


def _provenance(response, agent_name: str, agent_version: str) -> dict:
    return {
        "agent_name": agent_name,
        "agent_version": agent_version,
        "model": _response_model(response),
        "response_id": getattr(response, "id", None),
        "usage": _jsonable(getattr(response, "usage", None)) or {},
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dry_run": False,
    }


def _next_attempt(panel_dir, step) -> int:
    """1-based attempt number: how many log.json entries this step already has."""
    try:
        entries = json.loads(layout.log_file(panel_dir).read_text(encoding="utf-8")).get("entries", [])
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    return 1 + sum(1 for entry in entries if entry.get("step") == step)


class StepAgent:
    """A workflow step's Prompt Agent: prompt + ordered images + schema-valid answer."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.schema = load_schema(config.schema)
        self.example = load_json(config.example)
        self.validator = Draft202012Validator(self.schema)
        self._client = None
        self._verified_text_version: tuple[str, str] | None = None
        self.last_provenance: dict = {}

    @property
    def agent_name(self) -> str:
        if dry_run():
            return self.config.display_name
        name = os.environ.get(self.config.name_env, "").strip()
        if not name:
            raise RuntimeError(
                f"set {self.config.name_env} in .env for {self.config.step_id}; "
                "workflow.yaml does not supply a Foundry name fallback"
            )
        return name

    @property
    def agent_version(self) -> str:
        if dry_run():
            return "dry-run"
        version = os.environ.get(self.config.version_env, "").strip()
        if not version:
            raise RuntimeError(
                f"set {self.config.version_env} in .env for {self.config.step_id}; "
                "use an existing Foundry version or sync the agent with --apply --write-env"
            )
        return version

    def _get_client(self):
        if self._client is None:
            self._client = _openai_client()
        return self._client

    def _get_project_client(self):
        return _project_client()

    def prompt(self, values: dict) -> str:
        """Render local instructions, step data, and the local JSON contract."""
        rendered = render_prompt(
            self.config.prompt,
            {"example": self.example, "vocabulary": vocabulary(), "repair_feedback": "",
             "runtime_files": "- no runtime files declared", **values},
        )
        instructions = self.config.instructions.strip()
        if self.config.response_mode == "artifacts":
            return "\n\n".join(part for part in (instructions, rendered) if part)
        schema_request = (
            "Return exactly one JSON object and no Markdown fences or explanatory text. "
            "The JSON must satisfy this schema:\n"
            + json.dumps(self.schema, indent=2, ensure_ascii=False)
        )
        return "\n\n".join(part for part in (instructions, rendered, schema_request) if part)

    async def ask_artifacts(self, values, *, files, images=(), panel_dir,
                            step, report_name, dry_run_files=None, artifact_subdir=None,
                            download_citations=True):
        """Use unstructured Code Interpreter output; validate the downloaded report."""
        from src.workflow.artifact_agent import ask_artifacts

        return await ask_artifacts(
            self, values, files=files, images=images, panel_dir=panel_dir, step=step,
            report_name=report_name, dry_run_files=dry_run_files, artifact_subdir=artifact_subdir,
            download_citations=download_citations,
        )

    async def ask(
        self,
        values: dict,
        images=(),
        save_to: pathlib.Path | None = None,
        missing_defaults: dict | None = None,
    ) -> dict:
        """Send one request and return the schema-valid JSON answer.

        ``save_to`` is a step file (src/models/layout.py): ``<panel>/<step>/<name>``. The call's
        provenance - one attempt, ok or failed - is appended to that panel's log.json.

        Agents 01 and 02 use free-text JSON with local validation. If that first
        response is malformed, one correction request is made against the same
        pinned agent and recorded in the panel log.

        ``missing_defaults`` is deliberately narrow: it may supply a safe value
        only when a top-level key is absent.  The completed answer still passes
        the configured schema, and the normalization is recorded in provenance.
        This lets a stage convert an omitted optional-to-the-workflow ledger into
        explicit unresolved records without weakening the shared JSON contract.
        """
        text = self.prompt(values)
        log_panel_dir = log_step = None
        if save_to is not None:
            save_to = pathlib.Path(save_to)
            log_panel_dir, log_step = save_to.parent.parent, save_to.parent.name
        entry = {
            "step": log_step, "agent_name": None, "agent_version": None, "model": None, "created_at": None,
            "response_id": None, "usage": None, "attempt": _next_attempt(log_panel_dir, log_step) if log_panel_dir else None,
            "status": "failed", "error": None,
            "files_sent": [pathlib.Path(path).name for path in images], "dry_run": dry_run(),
        }
        def validate_output(output_text):
            normalizations = []
            cleaned_text = output_text.strip()
            if (self.config.response_mode == "unstructured"
                    and cleaned_text.startswith("```") and cleaned_text.endswith("```")):
                first_line, separator, body = cleaned_text.partition("\n")
                if separator and first_line.strip().lower() in {"```", "```json"}:
                    cleaned_text = body[:-3].strip()
                    normalizations.append({"action": "remove_json_fence"})
            try:
                candidate = json.loads(cleaned_text)
            except json.JSONDecodeError as error:
                preview = cleaned_text[:160].replace("\n", " ")
                raise RuntimeError(
                    f"Prompt Agent returned non-JSON output ({error.msg} at character "
                    f"{error.pos} of {len(cleaned_text)}; prefix={preview!r})"
                ) from error
            if (self.config.response_mode == "unstructured"
                    and self.config.number in {"01", "02"} and isinstance(candidate, dict)):
                for key in ("title", "description"):
                    if key in candidate and key not in self.schema.get("properties", {}):
                        candidate = dict(candidate)
                        candidate.pop(key)
                        normalizations.append({"action": "drop_root_schema_metadata", "key": key})
            if missing_defaults and isinstance(candidate, dict):
                candidate = dict(candidate)
                for key, value in missing_defaults.items():
                    if key not in candidate:
                        candidate[key] = copy.deepcopy(value)
                        normalizations.append({"action": "missing_top_level_default", "key": key})
            errors = sorted(self.validator.iter_errors(candidate), key=lambda error: list(error.path))
            if errors:
                details = "; ".join(
                    f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
                    for error in errors[:5]
                )
                raise ValueError(
                    f"{self.config.display_name} answer does not match {self.config.schema}: {details}"
                )
            return candidate, normalizations

        try:
            if dry_run():
                log.info("%s: dry run, answering with %s", self.config.display_name, self.config.example)
                answer = copy.deepcopy(self.example)
                self.last_provenance = {
                    "agent_name": self.agent_name,
                    "agent_version": self.agent_version,
                    "model": None,
                    "response_id": None,
                    "usage": {},
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "dry_run": True,
                }
            else:
                agent_name = self.agent_name
                agent_version = self.agent_version
                log.info(
                    "%s: invoking Prompt Agent %s version %s",
                    self.config.display_name,
                    agent_name,
                    agent_version,
                )
                entry.update(agent_name=agent_name, agent_version=agent_version)
                if self.config.response_mode == "unstructured":
                    if self._verified_text_version != (agent_name, agent_version):
                        base = await self._get_project_client().agents.get_version(
                            agent_name=agent_name, agent_version=agent_version,
                        )
                        definition = (base.definition.as_dict()
                                      if hasattr(base.definition, "as_dict") else dict(base.definition))
                        text_type = ((definition.get("text") or {}).get("format") or {}).get("type")
                        if definition.get("kind") != "prompt" or text_type != "text":
                            raise RuntimeError(
                                f"{self.config.display_name} pinned Foundry agent {agent_name} "
                                f"version {agent_version} must use free-text output; "
                                f"pin a text-output version in .env (found {text_type!r})"
                            )
                        self._verified_text_version = (agent_name, agent_version)
                async def request(prompt_text):
                    return await self._get_client().responses.create(
                        input=[{
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt_text},
                                *(_image_input(path) for path in images),
                            ],
                        }],
                        extra_body={
                            "agent_reference": {
                                "name": agent_name,
                                "version": agent_version,
                                "type": "agent_reference",
                            }
                        },
                        timeout=self.config.timeout_seconds,
                    )

                response = await request(text)
                self.last_provenance = _provenance(response, agent_name, agent_version)
                entry.update(
                    created_at=self.last_provenance["created_at"],
                    model=self.last_provenance.get("model"),
                    response_id=self.last_provenance.get("response_id"),
                    usage=self.last_provenance.get("usage"),
                )
                output_text = _response_text(response)
                try:
                    answer, normalizations = validate_output(output_text)
                except (RuntimeError, ValueError) as first_error:
                    if self.config.response_mode != "unstructured" or self.config.number not in {"01", "02"}:
                        raise
                    repair = {
                        "attempt": 1,
                        "status": "failed",
                        "trigger": str(first_error),
                        "trigger_response_id": self.last_provenance.get("response_id"),
                        "trigger_usage": self.last_provenance.get("usage"),
                        "response_id": None,
                    }
                    entry["repair_attempts"] = [repair]
                    repair_prompt = (
                        text
                        + "\n\nYour previous response failed local JSON validation. Regenerate the complete "
                          "answer from the source image. Return one corrected JSON object only; "
                          "do not use Markdown fences or commentary. Validation error: "
                        + str(first_error)
                    )
                    try:
                        repaired_response = await request(repair_prompt)
                    except Exception as repair_error:
                        repair["error"] = str(repair_error)
                        raise
                    repaired_provenance = _provenance(
                    repaired_response, agent_name, agent_version,
                    )
                    repair.update(
                        model=repaired_provenance.get("model"),
                        response_id=repaired_provenance.get("response_id"),
                        usage=repaired_provenance.get("usage"),
                    )
                    try:
                        answer, normalizations = validate_output(_response_text(repaired_response))
                    except (RuntimeError, ValueError) as repair_error:
                        repair["error"] = str(repair_error)
                        raise
                    repair["status"] = "ok"
                    self.last_provenance = repaired_provenance
            entry.update(agent_name=self.last_provenance["agent_name"], agent_version=self.last_provenance["agent_version"],
                        model=self.last_provenance.get("model"), created_at=self.last_provenance["created_at"],
                        response_id=self.last_provenance.get("response_id"),
                        usage=self.last_provenance.get("usage"))
            if dry_run():
                answer, normalizations = validate_output(json.dumps(answer))
            if normalizations:
                log.warning(
                    "%s: normalized response: %s",
                    self.config.display_name,
                    normalizations,
                )
                self.last_provenance["normalizations"] = normalizations
                entry["normalizations"] = normalizations
            if save_to is not None:
                save_to.parent.mkdir(parents=True, exist_ok=True)
                save_to.write_text(json.dumps(answer, indent=1, ensure_ascii=False), encoding="utf-8")
            entry["status"] = "ok"
            return answer
        except BaseException as error:
            entry["error"] = str(error)
            raise
        finally:
            if log_panel_dir is not None:
                layout.append_log(log_panel_dir, entry)
