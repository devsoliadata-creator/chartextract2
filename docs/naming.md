# Naming convention

One rule per kind of name, written once here and enforced by `tests/test_naming.py`. The workflow's Prompt Agent
calls follow it too: every agent prompt gets the exact vocabulary through `$vocabulary`.

## 1. Steps own their files

A step id is `agentNN_<verb>_<noun>` (a model call) or `python_<verb>_<noun>` (deterministic Python).
The step id names everything that belongs to the step:

| Thing | Name |
|---|---|
| code | `src/workflow/steps/<step_id>.py` |
| prompt | `prompts/<step_id>.md` |
| response schema | `schemas/<step_id>.schema.json` |
| example answer | `examples/<step_id>.example.json` |
| agent answer | `<figure_id>/<step_id>.json` (`agent02_check_extraction_round<N>.json` per loop round) |

## 2. Stages are named after the step that writes them

The extracted points exist in three stages, each written to its own `<panel>/<stage>/` folder
(`src/models/layout.py`: `STEPS`, `step_dir()`, `step_file()`, `images_dir()`, `image_file()`). A
stage file is just its bare name inside that folder (`src/models/naming.py`: `STAGES`,
`stage_file()` returns the bare part, e.g. `stage_file("agent04", "points.csv") == "points.csv"`):

| Stage | Written by | Files |
|---|---|---|
| `python` | `python_extract_points` | `python/points.csv`, `python/points.json`, `python/summary.json`, `python/audit.json` (metadata + hard checks + extraction audit), `python/images/` (`overlay.png`, `compare.png`, `redraw_diff.png`, `legend_markers.png`, `tick_check.png`, `tick_candidates.json`, `dense/`) |
| `agent03` | `agent03_review_points` | `agent03/answer.json`, `agent03/points.csv`, `agent03/points.json`, `agent03/audit.json` (metadata + hard checks + edit audit + recovery), `agent03/batches/`, `agent03/images/` (`overlay.png`, `compare.png`, `redraw_diff.png`, `contact_sheet.png`, `tiles/`) |
| `agent04` | `agent04_final_check` | `agent04/answer.json`, `agent04/points.csv`, `agent04/points.json`, `agent04/audit.json` (metadata + hard checks + edit audit), `agent04/images/` (`overlay.png`, `compare.png`, `redraw_diff.png`) |

Every model call and every attempt (dry run, ok or failed) is one entry in the panel's
`log.json` (`src/models/layout.py`: `log_file()`, `append_log()`), instead of a `*.provenance.json`
sidecar per call.

All agent image/file inputs are selected by editable `runtime/agent00.yaml` through `runtime/agent04.yaml`; Agent 03 and Agent 04 Code Interpreter inputs are selected by their respective runtime profiles. Each listed file uploads separately; the log records its
panel-relative or project-relative source path. Agent 03's profile omits `slot_native_*.png`,
`tile_*_native.png`, `clean_overlay.png`, and the candidate `review_points.json`.

User-facing files keep plain names: `<document>/final.csv` (every final row of a document),
`all_data*.csv`, `dashboard.html`, `review.html`, `panel.png`, `spec.json`, `qa.json`, `log.json`,
`error.txt` (only on failure).

## 3. Ids

| Id | Rule | Example |
|---|---|---|
| document | the input file name without extension (`config.yaml` `input.document_name`) | `combined_04` |
| figure / folder | `config.yaml` `naming.figure_id`; the folder name *is* the id | `fig2`, `fig3a`, `figp7` |
| panel | lowercase panel letter, `x` for a single-panel figure (`naming.panel_id`) | `a`, `x` |
| series | `<figure_id>:<panel_id>:<label slug>-<hash>` | `fig3a:a:co2-195k-d3fd5c48` |
| point | `pt-<hash>` | `pt-6a16ebd68e6ff815` |

## 4. Series labels

`<gas> <temperature>K[ <branch>]`, as the legend reads: `CO2 273K`, `CO2 273K ads`, `N2 77K`.
No space before `K`, `CO2` not `CO₂`. `naming.series_label()` normalises anything an agent writes
(`CO2 273 K` -> `CO2 273K`) before labels are compared; code never compares raw labels.

## 5. Vocabulary

Marker shapes, fills, line styles, axis scales, branches and verdicts come only from the enums in
`schemas/shared.schema.json` (`triangle_up`, never "up triangle"). Prompts show them through
`$vocabulary` (`src/workflow/resources.py`); nothing else defines them.

## 6. Spelling and keys

- American spelling in code, keys, config and docs: `color`, `color_hex`, `color_tolerance`.
- JSON/YAML keys and Python names are `snake_case`; CSV columns follow `schemas/points_csv.contract.json`.

## 7. Environment variables

| Prefix | For | Examples |
|---|---|---|
| `FOUNDRY_` | the Foundry project and Prompt Agents | `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_00_NAME`, `FOUNDRY_AGENT_00_VERSION` |
| `CHART_EXTRACT_` | this project's own switches | `CHART_EXTRACT_OUTPUT_DIR`, `CHART_EXTRACT_REVIEWS_DIR`, `CHART_EXTRACT_DRY_RUN` |

The required per-step Prompt Agent settings are `FOUNDRY_AGENT_<NN>_NAME` and `FOUNDRY_AGENT_<NN>_VERSION`, where `<NN>` is
derived from the `agentNN_...` step ID. `.env` supplies the actual values; `workflow.yaml` contains no duplicate names,
versions, or configurable environment-key aliases. `.env.example` lists each key. The local `agentNN` log/graph label is
derived from the step ID and is not the Foundry agent name. Desired model deployments use
the model configured on the pinned Foundry agent version.

## 8. Where things live

| Folder | Holds |
|---|---|
| `src/workflow/` | the workflow graph, agents, state; `steps/` one module per step |
| `src/tools/` | deterministic Python (extraction, checks, artifacts); no model calls |
| `src/models/` | naming and shared helpers |
| `prompts/`, `schemas/`, `examples/` | one file per agent step (rule 1) plus `shared.schema.json`, the CSV contract and the CO2 scope policy |
| `scripts/` | command-line utilities (review server, rebuild tables, setup) |
| `evaluations/` | the benchmark |
| `docs/` | documentation |

Only project files live in the repository root (`README.md`, `workflow.yaml`, `config.yaml`,
`main.py`, packaging and environment templates).
