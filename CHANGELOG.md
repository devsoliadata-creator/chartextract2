# Changelog

## 2026-10-05

- Fixed the master-table publish gate (`src/tools/table.py`): the current Agent 04 stage authority `agent04_inline_csv` is accepted (every run since the 2026-10-02 transport change had been marked `invalid_agent04` with zero rows). A failed Python hard check now keeps the table and flags review instead of discarding it. The points CSV contract is compared by major version instead of file bytes. Malformed legacy `line_sample` rows are dropped individually; the marker rows in the same table still publish. `aggregation_report.json` panels gain `final_flags` with the reason for any rejection.
- Made Agent 04 CSV validation non-fatal (`src/tools/qa_csv.py`, `src/workflow/steps/agent04_final_check.py`): invalid rows are recorded in `edit_audit.rejected_rows` instead of failing the panel; malformed report fields (verdict, series list, row_count, changes entries, unresolved_slots) are normalized and listed in `edit_audit.report_normalizations`. Any validation failure, not only omissions, triggers the single correction request, which now names the exact row errors. On the retry, Agent 03 rows are carried over unchanged for every candidate ID the reviewer omitted or returned invalidly (`carried_from_agent03=true` in notes, `edit_audit.carried_over`). The Agent 03 inventory fallback is reached only when nothing parses (for example a wrong CSV header). An unusable calibration object falls back to the Agent 03 calibration for any defect, not only a missing frame.
- Agent 04 now receives `panel.png` and `agent03/points.json` as Code Interpreter files (`runtime/agent04.yaml`); its prompt already required both.
- Added `tests/` with coverage for the publish gate and tolerant validation against the saved `combined_01/fig3a` panel.

## 2026-10-02

- Added `workflow.md` with the workflow stages, routing, artifacts, and file inventory.
- Updated Agent 03 review-tile generation to reuse identical slot crops and to make a full-height column crop when a predicted y position is outside the plot frame.
- Removed duplicate slot images from the `combined_04/fig2a` review bundle and updated its manifest to reference the retained crops.
- Changed Agent 04 final-table transport to return the complete canonical CSV and its SHA-256 digest inline with the JSON QA report; the workflow now persists and validates those authored bytes without downloading cited container output.
- Expanded the Agent 04 response contract and runtime evidence profile with the canonical CSV contract, Agent 03 report, and required source/review inputs.
- Preserved Agent 04's bounded omission-correction pass and Agent 03 inventory fallback for invalid or incomplete final tables.
- Fixed Agent 03 reruns by importing the shared `read_json` helper in the rerun workflow.
- Removed Agent 04's obsolete free-text preflight guard. Its configured reviewer remains the base definition for a short-lived Code Interpreter runtime, with panel inputs selected exclusively by `runtime/agent04.yaml` and mounted under their runtime filenames.
- Added editable runtime input profiles for Agents 00, 01, and 02, and removed their hard-coded image lists. Agent 00 names its candidate/page sources in `runtime/agent00.yaml`; Agents 01 and 02 resolve panel and Python-review images from their respective profiles.
- Updated Agents 00–02 prompts to name their runtime-selected inputs and identify `panel.png` by filename instead of assuming fixed image positions.
- Reconciled Agent 04's runtime profile with its prompt: restored the native panel, spec, Agent 03 CSV/JSON/report, response schema, and CSV contract inputs, and removed a duplicate candidate CSV entry.
