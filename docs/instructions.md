# Development and handover guide

This guide covers setup, normal operation, changes, reruns, and release checks. Treat `workflow.yaml` as the source of truth for the workflow graph and Prompt Agent configuration.

## Environment and first run

Use Windows PowerShell and Python 3.11 or newer. Install the requirements, sign in with Azure CLI, and configure the five pinned Prompt Agents in `.env`.

```powershell
.\scripts\setup.ps1
pip install -r requirements.txt
az login
python main.py --run <file-name>
```

The project supports Entra authentication and explicit API-key authentication. Keep credentials in `.env`; never add them to source, examples, reports, or commits.

## Step ownership

Every workflow step keeps the same ID across its implementation and contract files.

| Purpose | Location |
|---|---|
| Foundry connection and actual agent name/version pins | `.env` (`FOUNDRY_AGENT_<NN>_NAME` / `_VERSION`) |
| Graph, agent settings, and local instructions | `workflow.yaml` |
| Local paths and deterministic extraction/check settings | `config.yaml` |
| Step implementation | `src/workflow/steps/<step_id>.py` |
| Prompt and runtime placeholders | `prompts/<step_id>.md` |
| Agent 00–02 response contract | `schemas/<step_id>.schema.json` |
| Valid response example | `examples/<step_id>.example.json` |
| Shared vocabulary and definitions | `schemas/shared.schema.json` |
| Tests | `tests/` |

Agents 00–02 return strict JSON. Their schemas require every declared property and reject additional properties. Agent 03 returns its edit report through free-text output. Agent 04 returns its QA report and complete canonical CSV inline through free-text output; the CSV is persisted and validated locally. QA also validates the complete points CSV contract.

An agent step's `agentNN_...` ID determines its name/version environment keys. For example, `agent04_final_check` uses `FOUNDRY_AGENT_04_NAME` and `FOUNDRY_AGENT_04_VERSION`; the loader derives these keys, so `name`, `version`, `name_env`, and `version_env` do not belong in YAML. The `agent04` display label is local; the actual Foundry name may differ. Real requests require both configured values and never fall back to an old YAML version. Graph display and dry runs do not need them; dry-run provenance uses the local label and `dry-run` version.

`main.py` loads the repository `.env` with precedence over stale parent-shell values. Invocation uses the model stored in each pinned agent version. The example file leaves version pins blank because those numbers belong to the user's Foundry project. Keep existing `.env` pins during a configuration cleanup. Restart the process after edits because workflow, prompts, and schemas are cached.

If a prompt placeholder changes, provide it from the step and update its contract test. Agent 03/04 requests load the current local prompt, and their temporary immutable runtime versions use the current `workflow.yaml` instructions. These local reviewer policy edits therefore take effect on the next invocation without changing base pins. Changes to the pinned model, tools, response mode, or Agent 00–02 instructions/schema require a new pinned Prompt Agent version created and pinned in Microsoft Foundry.

## Evidence and extraction rules

Source images are authoritative. Python proposals and model suggestions are evidence to inspect; they do not become measurements by implication.

Keep extraction helpers deterministic and free of model calls. When a helper result is sent to a reviewer, label it as supporting evidence. Keep inset masks and CO2 scope explicit. Do not create coordinates from expected counts, fitted curves, or a duplicated inset. When a native marker cannot be separated, record an unresolved slot or a clearly marked low-confidence row.

Crowded, high-density, and overlapping regions use one row per logical source instance; one centroid must not substitute for multiple markers. When a localized marker-bearing cluster remains ambiguous between N and N+1 instances, choose N+1 once for that cluster and mark the uncertain extra as estimated, low-confidence, and requiring review. Preserve its identity across Agents 03/04 instead of adding another extra on each pass. Record the cluster/ROI, competing counts, source-based center/owner, and honest uncertainty. Missing Python support does not block this estimate; line-only or blank regions do not trigger this density rule. Keep evidence status in table values and retain the ordinary series marker style.

The explicit shared-zero rule is an additional estimation policy: when the source appears to show all eligible series starting at an in-frame linear-axis (0,0) origin but the first markers are unresolved, include one estimated origin marker for each affected series. Compute its native position from the calibration, label the shared-origin assumption and uncertainty, and require review. Agent 04 notes include `origin_estimate=true`; these remain ordinary series markers in the figure. Preserve resolved nonzero markers, reuse origin identities across reviewers, and count an origin estimate toward any extra for the same unresolved crowded start. Log axes, excluded/uncalibrated origins, and visibly nonzero starts do not qualify. Python applies the agent's chosen coordinates; it does not infer or insert origin points itself.

An explicit Agent 03 `overlap=true` positional edit with valid evidence and positive uncertainty can retain a separate instance within another marker's radius; the override is audited. For an overlapping reassignment, supply both coordinates and positive uncertainty even when retaining the current center. Agent 04 can retain distinct marker IDs at identical native coordinates when overlap provenance is explicit. Ordinary undeclared duplicates and duplicate trace samples remain invalid; do not move centers artificially to pass validation.

Agent 03 reviews the complete table and writes a JSON edit report. Local tools apply those edits and rebuild the candidate. Agent 04 may add, remove, move, or reassign rows and writes a complete replacement `points.csv` plus `answer.json`. QA checks row identity, eligible series, native support for changes, calibration, and unresolved-slot accounting. Invalid files remain available for inspection and do not silently become final output.

Agent 04 freezes and rereads its inline final CSV, derives counts and the stable-ID change list, and writes its report last with `points_csv_sha256`. This checksum binds the report to the exact UTF-8 CSV bytes persisted from its response. A glyph-only correction is not a `reassign`; changes in series ownership or evidence type are. Positional moves compare native pixel coordinates, not recalibrated x/y values.

If only report counts or the declared change inventory disagree, Python may preserve a uniquely identified, fully data-valid CSV for human review. It corrects redundant counts, retains the authored report and missing/extra change claims in the audit, and records the actual CSV diff without inventing explanations or changing measurements. Invalid rows, identity/evidence/calibration failures, hash mismatches, or ambiguous file selection still fail. Legacy reports without a checksum require a single unique CSV, or a unique valid file matching total and per-series counts. The original checksum stays with the cited artifact in the audit; stage CSVs are reserialized and have a separate digest in metadata.

Keep each checked Python round with its spec and `agent02_answer.json`. The live candidate remains the latest checked round, even if an earlier round has more detections. A retry-limit handoff records unexecuted corrections and requires review. Round scores and alternate snapshots are supporting material; never use them to overwrite Agent 02's structural state.

Save Python dense-band samples in `python/supporting_traces.json`, outside the candidate point inventory. Agent 04 may inspect these ordered native segments as supporting evidence, but its final CSV contains marker rows only; describe continuous line evidence and unresolved gaps in the report. Evidence-type changes require a `reassign` audit entry even when series identity stays the same. Python must not restore deleted rows after Agent 04.

## Calls, retries, and receipts

Each eligible panel receives one Agent 01 call, bounded Agent 02 check rounds, and one review call from Agents 03 and 04. Calls run sequentially.

Agent 03 and Agent 04 allow the configured long timeout for a full-file review. SDK retries remain disabled. A timeout or cancellation stops the call without replaying a paid request. Rerun the stage after inspecting the saved error.

Every model attempt writes one entry to the panel's `log.json`, including agent name and version, response ID, usage, status, timestamp, and error details. Temporary uploads are deleted after the attempt; Agent 04's inline CSV is retained as authored input in its stage folder.

## Rerun a saved document

```powershell
python main.py --rerun --document RUN --agent 03 --all
python main.py --rerun --document RUN --agent 04 --only
python main.py --rerun --document RUN --agent 01 --all
```

`--only` refreshes the selected stage and leaves later stages unchanged. The panel remains marked for review until downstream stages are refreshed. Agent 04 still refreshes final and aggregate tables when run alone. File-based reviewers make a fresh call on every rerun.

Use a new run when discovery, panel selection, inset handling, or crop geometry changes. Rerunning Agent 01 cannot repair an incorrect saved crop.

## Review and publication

Use `python scripts/review_server.py` to inspect and correct rows. Corrections are stored under `artifacts/reviews/` and the tables are rebuilt. `python scripts/rebuild_table.py` performs the rebuild without starting the server.

The aggregate report is complete only when every panel reaches `complete_against_reviewed_evidence`. A `review`, `partial_review_required`, `blocked`, or `failed` panel must remain visible in the handover report.

