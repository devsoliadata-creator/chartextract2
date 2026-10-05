# Project memory: CO2 chart extraction

Owner: dev.soliadata@gmail.com. Last updated 2026-10-05.

## What this is

A Python workflow that extracts CO2 isotherm data points from research-paper
figures (PDF or image) and writes reviewable CSV / JSON / HTML outputs.
Python does the deterministic work locally; five pinned Azure Foundry
"Prompt Agents" (Agent 00-04) do the vision/judgement steps.

Current source of truth: https://github.com/julliamckenna/chart_extract_v5.1
(branch `main`, single commit "first commit"). This repo, `chartextract2`,
is the user's own copy/working repo for it.

Original dev workspace was Windows: `C:/Users/jmckenna/dev-ai/AIP-RD1-V.3.5`,
PowerShell, `.venv/Scripts/python.exe`. Setup scripts are `.ps1`.

## Pipeline (one document at a time)

1. `python_read_document`  - read PDF/image, inventory figure candidates
2. `agent00_find_figures`  - pick CO2 isotherm panels (JSON-schema output)
3. `agent01_read_chart`    - axes, units, log/linear, ticks, legend, series -> `spec.json`
4. `python_extract_points` - calibrate pixels->values, detect markers, assign series
5. `agent02_check_extraction` - bounded check loop; may patch spec, Python reruns
6. `agent03_review_points` - independent source-pixel review, edit report
7. `agent04_final_check`   - final evidence review, may replace the whole table (returns full CSV + SHA-256)
8. `python_publish_results` - validate, write final.csv, all_data*.csv, dashboard.html, review.html

Routes between steps: `continue`, `next_panel`, `adjust` (back to step 4), `publish`.
One bad panel never stops the document. Everything is written to disk per stage.

## Key rules of the domain

- Source pixels are the authoritative evidence. Never fabricate marker coordinates.
- Only native-supported marker cores become rows. Unresolved dense regions stay unresolved.
- CO2-only scope. Excluded series (e.g. N2) never produce rows.
- Dense line traces go to `python/supporting_traces.json` as `line_sample`,
  `marker_shape=none`, never joined across gaps.
- Insets are ignored. Broken axes and double y-axes need manual handling.
- Hard numeric checks live in `src/tools/hard_checks.py` (Qst 10-60 kJ/mol etc).

## Where things live

| Thing | File |
|---|---|
| Foundry endpoint + agent NAME/VERSION pins | `.env` (never commit) |
| Step graph, timeouts, prompt/schema paths | `workflow.yaml` |
| Folders, naming, extraction tuning, numeric checks | `config.yaml` |
| Per-agent input file selection | `runtime/agent0N.yaml` |
| Prompts | `prompts/agent0N_*.md` (proposals in `prompts/proposals/`) |
| Only code that calls Foundry | `src/workflow/agents.py` |
| One module per step | `src/workflow/steps/<step_id>.py` |
| Deterministic extraction library | `src/tools/` |
| Run outputs | `artifacts/runs/<document>/<figure_id>/` |
| Workflow narrative | `workflow.md`, `docs/domain/architecture.md` |
| Dev rules | `docs/instructions.md` |

Change rule: keep a step ID aligned across `workflow.yaml`, `src/workflow/steps/`,
`prompts/`, `schemas/`, `examples/`, and tests. Schema change = update prompt,
example, and contract tests together.

## Commands

```
python main.py --run paper.pdf              # full run (file in data/input, path, or URL)
CHART_EXTRACT_DRY_RUN=1 python main.py --run paper.pdf   # no Azure calls, wiring check
python main.py --graph                      # print Mermaid graph
python main.py --rerun --agent 03 --all     # rerun from a saved stage
python -m pytest -q
python scripts/review_server.py             # human review UI
python scripts/rebuild_table.py
```

Auth: `az login`, identity needs **Azure AI User** role on the Foundry project.
Or `FOUNDRY_AUTH_MODE=api_key` + `FOUNDRY_PROJECT_API_KEY`.

## Repo layout decision (2026-10-05)

- `chartextract2` (this repo) is the WORKING repo: all fixes land here.
- `julliamckenna/chart_extract_v5.1` is READ-ONLY upstream: pull from it, never
  push to it or open PRs there. Imported at upstream commit 1958958.
- Full diagnosis of the point-loss / error problems: `docs/diagnosis-2026-10-05.md`.
- Dev loop: create a venv, `pip install -r requirements.txt`, `python -m pytest -q`.
  A dry run (`CHART_EXTRACT_DRY_RUN=1 python main.py --run <image>`) exercises the
  graph without Azure; it stops at Python calibration when the example answers do
  not match the image, which is expected.

## Known state / gaps (as of 2026-10-05)

- Fixed here (see CHANGELOG 2026-10-05): publish gate rejecting every Agent 04
  table, fatal single-cell CSV validation, Agent 04 missing `panel.png` input.
- Still open from the diagnosis: Agent 03 has no table authority (diff-only ops,
  measured centers never become rows), `accept`/DONE unreachable by design,
  Agent 02 round 2 forces review, prompts too long and contradictory, Python
  extractor recall.
- `schemas/` now present. `tests/` from the original workspace never arrived;
  the `tests/` here were written fresh in this repo. `data/` and `evaluations/`
  are still absent. No `.gitignore` in the repo.
- Open problem being worked: dense/overlapping marker extraction
  (see `docs/domain/density_fix_handoff.md`). Agent 03/04 prompts were
  rewritten 2026-09-28 to do independent native measurement.
- Suggested future architecture change: give Agent 03 a full candidate-CSV +
  calibration interface like Agent 04 (needs schema/host/audit changes).
- Suite was at 60 passing tests at last record.

## User preferences for this project

- User is strong on Power Platform / data modeling / low-code, not on git or CLI.
  Give exact commands and say where files go.
- Prefer doing the work directly over giving manual instructions.
- Never commit `.env`, secrets, or generated run output.
