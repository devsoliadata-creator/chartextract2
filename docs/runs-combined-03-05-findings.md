# Findings from runs combined_03, combined_04, combined_05 (reviewed 2026-10-05)

Twelve panels were attempted across the three runs. Only 3 reached an Agent 04 table.

| run / panel | ended at | why |
|---|---|---|
| combined_03/fig10 | Agent 01 | answer had extra root `title`/`description` fields, schema rejected it (current code strips these) |
| combined_03/fig3a | Agent 01 | same |
| combined_03/fig2a | Agent 01 | model echoed the schema instead of an answer, then connection error, then auth error; 4th attempt OK but panel stayed failed |
| combined_03/fig2b | Agent 04 | OK: 121 rows, review. 6 Agent 02 rounds, 3 Agent 03 calls (reruns) |
| combined_04/fig2a | Agent 04 | Agent 03 364 rows. Agent 04 failed 6 times: wrong version pin (x2), old free-text guard (x2), PNG rejected as "context stuffing" input (x1, interim code), one blank error |
| combined_04/fig2b | Agent 01 | failed, no error text recorded |
| combined_04/fig3a | Agent 04 | OK after 13 Agent 04 attempts: 3 answers discarded as "no schema-valid JSON report" (180k tokens, raw text not saved), 5 auth failures, calibration fallback. 66 rows |
| combined_05/fig10 | Agent 01 | 401 token expired |
| combined_05/fig2b | Agent 01 | 401 token expired |
| combined_05/fig3a | Python | "legend: no colored symbols found" after 2 Agent 02 rounds; no fallback to Agent 01 colours |
| combined_05/fig2a | Agent 04 | Agent 03 388 rows. Agent 04 answered (173k tokens) with only 43 CSV rows while claiming 395, and a `frame_px` dict instead of a list. Correction call hit 401. Panel failed, everything lost |

## What the evidence says

1. Auth token expiry is the single biggest killer in these runs: 8 of the failed agent attempts and 3 whole panels (combined_05) died on 401 / DefaultAzureCredential. Out of scope for the code fixes, but nothing downstream can work until a run survives its own length.
2. Agent 04 does not return full tables for big panels. combined_05/fig2a: 43 rows returned, 395 claimed. The inline-CSV contract asks a model to re-emit ~400 rows x 31 columns; it emitted the rows it touched. With the old code that is an omission error, then a second 173k-token call. With the new carry-over (this repo) the same answer publishes 386 rows: 43 from Agent 04 + 343 carried from Agent 03.
3. Schema-invalid Agent 04 answers are discarded with no feedback. The code should write `agent04/response.txt` on failure, but no such file survives in combined_04/fig3a, so the three discarded answers (180k tokens) cannot be inspected.
4. Python legend detection has no fallback. Agent 01 already supplies `color_hex` per series; `extract.py:477` raises instead of using it.
5. Agent 01 schema strictness (extra root fields) killed 2 panels in combined_03; the current code already tolerates this.
6. Token cost: 476k / 872k / 425k tokens per run; 180k on failed attempts in combined_04.
7. Where tables exist they are reasonable: combined_03/fig2b (121 rows, 6 series, dense low-pressure bands partly estimated), combined_04/fig3a (66 rows, steep rise well covered, inset ignored), combined_05/fig2a Agent 03 (388 rows across 11 series, the dense left fan is the gap).

## Re-aggregation with the fixed publish gate

| panel | before | after |
|---|---|---|
| combined_03/fig2b | 121 review rows | 121 review rows |
| combined_04/fig2a | 0 (invalid_agent04) | 364 Agent 03 rows in review (missing_final) |
| combined_04/fig3a | 0 (invalid_agent04) | 66 review rows |
| combined_05/fig2a | 0 (invalid_agent04) | 388 Agent 03 rows in review (missing_final); 386 if the Agent 04 answer is replayed through the new loader |

## Fix candidates raised by these runs (not yet done)

- Save the raw model text for every failed Agent 03/04 attempt, and repair schema-invalid reports (strip unknown keys, fill defaults) before discarding.
- Fall back to Agent 01 `color_hex` when legend symbol detection finds nothing.
- Change the Agent 04 contract to "return only changed and added rows plus deletes" officially, since that is what the model does for large panels; the carry-over already makes the host handle it.
