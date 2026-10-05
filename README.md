# Chart extraction workflow

This project extracts CO2 isotherm points from research-paper figures and writes reviewable CSV, JSON, and HTML outputs. The workflow runs locally. Only Prompt Agent calls use Azure Foundry.

## Start here

The supported environment is Windows PowerShell with Python 3.11 or newer, Azure CLI, and a Foundry project containing five pinned Prompt Agents. Your identity needs the **Azure AI User** role on that project.

```powershell
.\scripts\setup.ps1
pip install -r requirements.txt
az login
```

Create `.env` from `.env.example` and set the project endpoint plus the name and version for Agents 00 through 04. Keep `.env` private. Entra authentication is the default. API-key mode is available when you set `FOUNDRY_AUTH_MODE=api_key` and `FOUNDRY_PROJECT_API_KEY`.

Put a PDF or image in `data/input/`, then run:

```powershell
python main.py --run paper.pdf
```

For a local wiring check without Azure:

```powershell
$env:CHART_EXTRACT_DRY_RUN=1
python main.py --run paper.pdf
$env:CHART_EXTRACT_DRY_RUN=0
```

## How a run works

Configuration has one owner for each setting:

| File | Settings |
|---|---|
| `.env` | Foundry connection, actual agent names and pinned versions; desired model deployments for syncing |
| `workflow.yaml` | Step order, timeouts, response mode, role instructions, and prompt/schema paths |
| `config.yaml` | Local folders, naming patterns, extraction tuning, and numeric checks |
| `runtime/agent00.yaml` through `runtime/agent04.yaml` | Individual files/images selected for each agent stage |
| `prompts/` | Detailed task instructions sent with each reviewer request |

For `agent04_final_check`, the workflow automatically reads `FOUNDRY_AGENT_04_NAME` and `FOUNDRY_AGENT_04_VERSION`. YAML no longer repeats `name`, `version`, `name_env`, or `version_env`. Missing pins fail explicitly on real requests; dry runs and graph display need no Foundry identity. Restart the process after configuration edits.

Agent 03/04 local prompt and role-instruction edits take effect on the next invocation in a fresh process. `--apply --write-env` is needed when creating/updating a pinned base agent, such as changing its model or tools; it is not needed for these local reviewer policy edits.

Agent 00 uses Foundry JSON schema output. Agents 01/02 use pinned free-text versions and return JSON that is validated against the local schema. The workflow invokes those pins directly, accepts a JSON fence or extra root `title`/`description` fields, and makes one correction request if the answer is still invalid. Each response and correction is recorded in the panel's `log.json`. Every agent stage reads its image/file inputs from its editable [`runtime/`](runtime) profile. Agents 03/04 additionally mount their selected files in a short-lived `chart-review-*` Code Interpreter runtime.

1. Python reads the document and inventories possible figures.
2. Agent 00 selects CO2 isotherms and identifies panels.
3. Agent 01 describes axes, legends, series, and panel geometry.
4. Python calibrates the axes and proposes marker points.
5. Agent 02 checks calibration and extraction quality through bounded rounds.
6. Agent 03 reviews the complete table and writes an edit report.
7. Agent 04 performs the final evidence review and can replace the full table.
8. Python validates and publishes the tables, reports, and review pages.

Source pixels remain the authoritative evidence. Python detections, overlays, redraws, and curve fits support review and diagnostics.

The structure and candidate table from Agent 02's latest checked round stay together. Python round scores are diagnostics; they cannot restore an earlier spec or override the reviewer. Agent 02 makes one complete plan and may issue one final targeted patch; Python executes that patch once before independent review.

Dense continuous bands are saved separately as `python/supporting_traces.json`, with native segment order and uncertainty. They do not add experimental marker rows. Agents 03/04 use this evidence to inspect crowded regions and recover actual markers. A trace retained by Agent 04 uses `evidence_type=line_sample`, `marker_shape=none`, low confidence, and inferred status; optional `segment_id` and `trace_order` notes define a reviewed path. Final reconstructions keep those lines separate from marker glyphs and do not join markers across unresolved gaps.

## Useful commands

```powershell
python main.py --graph
python -m pytest -q
python scripts/review_server.py
python scripts/rebuild_table.py
python main.py --rerun --agent 03 --all
python main.py --rerun --document RUN --agent 04 --only
python -m evaluations.telemetry_usage
```

Use `--document` to select one saved run. Use `--only` for one stage or `--all` to continue through downstream stages. Discovery, panel selection, and crop changes require a fresh run.

## Outputs

Run output lives under `artifacts/runs/`, or under `CHART_EXTRACT_OUTPUT_DIR` when configured.

- `final.csv` contains the panel's final rows when the panel reaches publication.
- `all_data.csv` contains the aggregate rows.
- `all_data_accepted.csv` contains rows accepted against reviewed evidence.
- `all_data_review.csv` contains rows that still need review.
- `dashboard.html` and each panel's `review.html` provide the review UI.
- Each panel stores stage files under `python/`, `agent03/`, and `agent04/`, including `points.csv`, `points.json`, `audit.json`, and supporting images.
- `agent03/measurement_targets.json` identifies native dense regions to inspect. `agent03/answer.json` records measured panel-pixel centers linked to retained point IDs or positional edits; incomplete measurements remain in the audit and require review.
- `log.json` records every model attempt, its version, usage, status, and error information.


## Change rules

Keep the step ID aligned across `workflow.yaml`, `src/workflow/steps/`, `prompts/`, `schemas/`, `examples/`, and tests. Keep model calls in `src/workflow/agents.py`; keep deterministic extraction helpers in `src/tools/`.

When a schema changes, update its prompt, example, and contract tests together. Never commit `.env`, secrets, or generated run output. Follow [docs/instructions.md](docs/instructions.md)

## Current limits

Figures are rendered as images. Log axes are supported. Broken axes and double y-axes require manual handling. Dense, fused, hidden, or overlapping markers can remain unresolved and should stay marked for review.
