# Changelog

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
