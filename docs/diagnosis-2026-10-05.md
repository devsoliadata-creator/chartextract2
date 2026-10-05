# Why points go missing and runs error out (diagnosis, 2026-10-05)

Scope: Python code, prompts, examples, saved runs in chart_extract_v5.1. Model calling and auth untouched.

## Bottom line

Across all 13 saved panels: 0 accepted rows, 11 panels `invalid_agent04`, 2 `partial_review_required`.
The pipeline is throwing away its own work at the end, and losing markers at every stage before that.
The intended design (03 has full authority, Python builds table from 03, 04 final authority, Python
recreates from 04) is NOT what the code does.

## A. The "errors": the pipeline rejects its own Agent 04 output

1. Authority label mismatch (newest bug, kills every run since 2026-10-02).
   agent04_final_check.py:410 writes authority="agent04_inline_csv".
   table.py:328 only accepts "agent04_code_interpreter_csv" or "agent03_inventory_repair_fallback".
   => every successful Agent 04 panel is "invalid_agent04" with no reason text. This is combined_04 fig2a/fig3a.
2. Contract fingerprint: table.py:351 rejects every older agent04/points.csv whenever
   schemas/points_csv.contract.json changes by one byte. 4 panels dead this way.
3. Legacy line_sample check (table.py:299): 3 panels dead (fig2a 557 rows, f2b 82, f2b_02 71).
4. Hard-check gate: table.py:334 drops the whole panel if the axis-range check fails, while
   agent04_final_check.py:448 calls the same failure a warning and keeps the table.
5. All-or-nothing CSV validation: qa_csv.py raises FinalCSVError on ONE bad cell in a 300-row CSV;
   agent04_final_check.py:278 only catches CandidateOmissionError. Everything else -> panel FAILED,
   no fallback to the Agent 03 table, 100k tokens discarded. Common triggers: inherited line_sample /
   template_fill rows, an Agent 03 "estimated" row with blank uncertainty, pixel precision drift
   counted as an uncited "move", "CO2 273 K" vs "CO2 273K" label, scale change, blank color_hex.
6. "accept" is unreachable on real charts: prompt says never accept while an estimated_marker row
   remains, and the same prompt demands N+1 estimated rows in every crowded cluster.
   => verdict always "review" -> status partial_review_required -> complete_panel_count = 0 forever.
7. Agent 02 round 2 always sets needs_review=True (agent02_check_extraction.py:399-401,
   max_check_rounds=2). Any panel Agent 02 did not approve on round 1 can never reach DONE.
8. Agent 04 is told to open /mnt/data/panel.png and to `reject` if it is missing
   (prompts/agent04_final_check.md:24,128). runtime/agent04.yaml never uploads panel.png.
   It only gets the image inline, shrunk to 1600 px (workflow.yaml max_image_side).
9. Run 04/figp1a: Agent 04 answered (99k tokens) then the 401 hit while downloading cited files.
   That transport was replaced on 10-02; auth itself is out of scope here.

## B. The "missing points": where markers are lost, stage by stage

Python extractor (src/tools/extract.py)
- Detector accepts only isolated, clean markers: area window 0.45-1.25 disc (1054), twin split
  capped at 2.05 disc (1062), circle fit needs 70% inliers (1116), band test (1147). Anything with a
  neighbour inside 1.5 r is rejected by design. That is exactly the crowded low-pressure region.
- Open (hollow) markers: r_est<2.5 returns [] for the series (963); hole filling only runs in
  hard_checks, not detection; Hough ring path only for direct-label charts (3139).
- Ownership passes DELETE instead of deferring: global_spatial_ownership tie removes all competitors
  (1779), settle_twins hypotheses lost without a count (1470/3277), series_gate drops (series_gate.py:138),
  "ambiguous:" rows not counted in unresolved_total (3500).
- Same-colour series: joint assignment rebuilds from raw candidates (3311), discarding twin splits.
- Recovery modules barely fire: dense resolver needs >=2 confident owners per column
  (dense_regions.py:113,122); template_fill needs 0.15 margin over rivals, impossible for same colour.
- ~300 hard-coded constants in extract.py, 22 config knobs, plus dense_low_pressure_fraction read
  but absent from config.yaml.
- round_score picks per-series "best round" by precision over count; a round with fewer rows wins.
  Saved runs: Python overrode Agent 02's latest round in 4/5 panels.

Agent 03 (src/workflow/steps/agent03_review_points.py, stage_edits.py)
- Agent 03 does NOT produce a table. Python applies its add/move/delete/reassign ops as a diff on the
  Python table (575,595). A marker exists only if Python found it or Agent 03 wrote a valid `add`.
- dense_measurements[].centers (the measured pixel centers) are NEVER converted to rows.
- Evidence: Agent 03 only ever deleted. 81 deletes, 7 adds attempted, 1 survived, 0 moves, 0 reassigns.
- series[].final_count from the agent is overwritten with the applied count (591-593): the gap
  between what it saw and what landed is hidden.
- Dense validation (dense_measurement_review.py:172,218,232,248) fires the second 900 s call almost
  always (status "unresolved" is always a problem; exact string label match; any edit in an ROI without
  a linked center). That second call has no try/except (546): timeout or schema fail = panel FAILED and
  the valid first answer is thrown away.
- Batching code (lines 30-195, 286-328) is dead; one call must cover the whole panel.
- Prompt is ~3,360 words + 364 words role text, with pressure against adding ("N+1 once", "fits do not
  establish markers").

Agent 04 (agent04_final_check.py, qa_csv.py, prompt)
- Prompt ~3,750 words + 385 role words, internally contradictory (choose N+1 vs do not force count up;
  keep every ID vs never write line_sample rows; overlap_flag required vs only for actual overlap).
- Old prompt/contract made it emit line samples instead of markers: fig2a 232 direct markers vs its own
  estimate of 323, plus 557 line rows. f2b/f2b_02: ~5 markers per fused series missing, 71-82 line rows.
- 131 marker->line reclassifications unaudited (audit says reassigned=0).
- examples/agent04_final_check.example.json is never shown to the agent and is itself invalid
  (header-only CSV, row_count 25 vs series sum 33).
- sha256 is never enforced (host overwrites it before validation).
- Token cost: f2b panel = 742k tokens over 22 calls; reasoning is ~99% of output tokens for 03/04.

Agent 02
- sample_band_at_columns / shared_x_columns are no-ops (extract.py:3496,3581) though the prompt promises them.
- A partial `series` list in its answer DELETES the series it omits (agent02 step 199-216).

Repo state
- schemas/, tests/, data/, evaluations/ not in GitHub. stage_artifacts.py:26 imports the contract at
  import time, so the GitHub clone cannot start. Last recorded test state: 117 pass, 1 fail.

## C. Fix plan (ranked by payoff / effort)

1. Publish gate (hours): accept "agent04_inline_csv" in table.py:328; make hard-check failure a flag not
   a drop; replace the byte fingerprint with schema_version only; stop rejecting legacy line_sample
   rows (drop them, keep markers). Rerun rebuild_table.py: existing runs immediately yield rows.
2. Make Agent 04 failures non-fatal (hours): catch FinalCSVError, write agent04 table from the rows that
   validate + list rejected rows in audit, fall back to Agent 03 table. One bad cell must not zero a panel.
3. Give Agent 04 the native panel (minutes): add panel.png to runtime/agent04.yaml.
4. Make Agent 03 match the intended design (days): let it return a full replacement CSV like Agent 04
   (same contract, same validator), with Python's table as reference input. Convert dense_measurements
   centers into rows meanwhile. Wrap the repair call in try/except and keep the first answer.
5. Make "accept"/DONE reachable: drop the "never accept with estimated rows" rule, or rename the final
   status so review rows still export as accepted-with-flag. Remove the unconditional needs_review in
   Agent 02 round 2.
6. Prompts: cut 03/04 to ~800 words each, one rule per topic, remove the contradictions listed above.
7. Extractor recall (weeks, optional once 03/04 own the table): never delete in ownership passes,
   fill holes for open markers, split fused blobs by area ratio, rank round_score by count before precision.
