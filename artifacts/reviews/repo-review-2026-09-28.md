Recommendation: selectively roll back the trace-as-marker behavior and restore agent control of structure selection. Keep telemetry and the useful evidence-preparation work. A full reset to HEAD would also restore behavior that conflicts with the intended architecture.

This review compares the current working tree with HEAD, inspects saved source images and stage outputs, and runs local checks. No implementation, configuration, prompts, saved runs, or commits were changed. This note is the only review deliverable added. No paid model calls were made.

The intended ownership is sound: Agents 00/01 establish scope and structure; Agent 02 verifies structure and calibration against source pixels; Python prepares crops, measurements, candidate hypotheses, and diagnostics; Agent 03 performs extraction; Agent 04 independently adjudicates the complete result. Source pixels establish evidence. Python can enforce file integrity and arithmetic consistency without deciding that a particular detection is an experimental measurement.

The saved output establishes the following. “Direct” below means rows labeled visible_marker or partially_visible_marker, not independently verified ground truth.

| Saved panel and stage | Direct markers | Estimated markers | Line samples | Template rows | Total |
|---|---:|---:|---:|---:|---:|
| combined_01 / fig2a / Python | 390 | 0 | 0 | 10 | 400 |
| combined_01 / fig2a / Agent 03 | 345 | 0 | 0 | 6 | 351 |
| combined_01 / fig2a / Agent 04 | 232 | 11 | 557 | 0 | 800 |
| combined_01 / fig3a / Agent 03 | 52 | 0 | 27 | 0 | 79 |
| combined_01 / fig3a / Agent 04 | 46 | 1 | 12 | 0 | 59 |
| f2b / figp1b / current Agent 04 | 96 | 1 | 82 | 0 | 179 |
| f2b_02 / figp1b / current Agent 04 | 102 | 0 | 71 | 0 | 173 |

For combined_01/fig2a, Agent 04 reclassified 107 existing direct-marker rows as line_sample, deleted six direct-marker rows and six template rows, and added 450 line samples plus 11 estimated origins. Its final count rose from 351 to 800 while direct-marker count fell from 345 to 232. This verifies a change in extraction behavior; it does not prove all 113 former direct-marker claims were valid measurements.

The 557 line samples in this panel were authored by Agent 04. The selected Python candidate contained no line_sample rows. The prompt is therefore a central cause here, not just the new Python band sampler.

1. **High priority: reverse the policy that turns line samples into marker glyphs.**

   Relevant locations: prompts/agent04_final_check.md:42, :55, :68; src/tools/stage_artifacts.py:558; src/tools/qa_csv.py:303; src/tools/recreate.py:151; src/tools/review_page.py:671; schemas/points_csv.contract.json:47.

   The current prompt explicitly requires samples across gaps larger than approximately one marker width, including continuous ink in otherwise marker-bearing series. It also requires trace rows to inherit the series marker. The contract and validator enforce that choice, and both plotting paths draw the glyphs. This explains circles, triangles, and stars appearing where the source provides a line. A correct line_sample row using marker_shape=none is currently rejected for a series with another marker style.

   Remove the mandatory gap-filling instructions from Agents 03/04. Restore marker_shape=none for line samples, separately from series identity. Keep trace proposals in a supporting trace artifact or layer, with an explicit evidence type and independent counts. Render a reviewed trace as a line without experimental marker glyphs. Retain the native-source trace sampler as an optional diagnostic rather than automatically merging its output into the experimental point inventory.

   This change must be coordinated across the prompts, CSV contract/example, builder, validator, renderers, and tests. Reverting only the renderer would leave Agent 04 manufacturing a large sampled table. Reverting only the prompt would leave the validator forcing marker styles on any remaining traces.

2. **High priority, pre-existing architecture problem: Python's round score overrides the structural reviewer.**

   Relevant locations: src/tools/round_score.py:26; src/workflow/steps/agent02_check_extraction.py:236, :396, :409.

   restore_best_round copies an older round over python/ and restores its spec.json at the panel root. This happens even when Agent 02 is satisfied with the latest round. Python's count score therefore selects the structure that Agents 03/04 receive.

   In combined_01/fig2a, round 1 had no expected-count checks and scored its raw 400 rows. Round 4 had 22 expected-count checks and scored 348 capped rows, with 359 total candidates. Round 4 had better diagnostic recall (0.872 versus 0.803), but the count comparison restored round 1. The restored spec had zero complex regions rather than round 4's two, and some series marker styles differed. The agent02/answer.json supplied downstream still describes the latest check rather than the restored candidate. These are different review states.

   Have Agent 02 select or approve the structural state. Keep older Python rounds as alternative proposals for review rather than replacing the agent-verified spec. If numerical ranking remains as a recommendation, use comparable regions and a consistent denominator across rounds; do not compare an uncapped initial total with later capped counts. Preserve the matching spec, calibration, candidate, and reviewer answer as one snapshot. This is not a reason to assume every later correction is correct; the reviewer must decide from source evidence.

3. **High priority: restore the missing Agent 04 recovery and changed-row evidence instructions.**

   Relevant locations: prompts/agent04_final_check.md:30–34 and :55–59; src/tools/qa_csv.py:356–386 and :519–536.

   The uncommitted prompt removes the detailed paragraph on native contour, symmetry, partial glyphs, excluded marks, and the conditions for estimated markers. It also removes the explicit new/moved-row notes recipe. The remaining prompt still refers to an estimated-marker rule in “Phase 3,” but that rule is no longer there. It requests validation of changed-row notes without supplying the former precise recipe.

   Meanwhile, the new validator correctly treats a change of evidence_type as an auditable reassignment, but the prompt does not clearly require that entry. Restore the recipe for evidence_kind, evidence_ref, source_evidence, and positive uncertainty_px, including native_trace when appropriate. Explicitly require an audit entry for marker-to-trace or trace-to-marker reclassification. Preserve native-supported partial markers; a fused band is not automatically an individual marker, and a visible marker must not be demoted merely to simplify the table.

   Local replay with the current validator rejects the saved combined_01/fig2a final CSV at row 12: “reassignments require native evidence, reference, and evidence kind notes.” Its saved audit reported zero reassignments despite 107 marker-to-trace conversions. The saved fig3a final CSV passes the current validator. Thus the saved fig2a run is not a clean verification of today's working tree. Keep the stronger evidence-change audit; update the prompt to satisfy it.

4. **Medium priority: remove misleading connecting lines from the reconstruction.**

   Relevant locations: src/tools/recreate.py:101, :116–135, :151–162.

   The multi-series reconstruction connects only non-trace points, while drawing the trace samples as separate markers. In combined_01/fig2a, estimated origins are connected directly to the next retained marker across the crowded rise. The resulting diagonal chords visibly disagree with the source curves even though trace samples lie elsewhere along those curves. The single-series evidence-view safeguard is disabled when there is more than one series.

   Use an evidence view for any series count. Draw experimental markers as markers, estimated positions distinctly, and reviewed trace segments as lines. Do not connect across unresolved regions or assume sorting by x establishes a native path. Native band samples need ordered segment identity, especially for steep branches and adsorption/desorption overlaps.

5. **Medium priority, pre-existing preparation defect: unknown slot coordinates produce crops at the left image margin.**

   Relevant location: src/tools/adjudicate_tiles.py:576.

   A missing column_px is converted to zero. combined_01/fig3a's slot:0001 and slot:0002 consequently reference [0,68,33,497], outside the plot whose left edge is about 118 pixels. Agent 04 explicitly reports these useless slot crops. A correct wider crowded strip is also supplied, so the source evidence is not entirely unavailable, but the focused slot assistance fails.

   Preserve unknown as unknown. Use the slot's native ROI or the appropriate crowded strip, clipped to the calibrated frame and masks. Include the crop-to-panel transform. This improves Python's supporting material without giving it authority to place a point.

6. **Medium priority: retain overlap hypotheses without declaring two observations from one core.**

   Relevant locations: src/tools/dense_regions.py:246–257; src/tools/extract.py:1713–1719; src/tools/stage_artifacts.py:420–431.

   Preserving overlapping candidates is useful for recall. However, the new resolver retains same-location cores for multiple series and the ownership check bypasses collisions whenever either proposal has dense_region provenance. The local-core check measures native ink, not independent multiplicity for every same-colored series. Downstream overlap_flag maps these candidates to partially_visible_marker with is_inferred=false.

   Keep alternatives and their competing ownership in the evidence bundle. Distinguish a candidate hypothesis from an agent-confirmed observation. Do not restore blanket coordinate deduplication, since real overlap is possible. Require Agent 03/04 to establish each logical instance from native evidence before the final table asserts multiple measured points.

The selective keep/undo plan is:

| Change | Recommendation |
|---|---|
| Telemetry rework | Keep. It is separate from the extraction-policy regression. |
| Native crops, manifests, source references, calibration diagnostics | Keep and repair the unknown-slot crop case. |
| Python band sampler | Keep as optional supporting trace evidence; remove automatic insertion into experimental marker inventory. |
| Mandatory trace gap filling and trace rows using marker glyphs | Undo across prompt, contract, validator, and presentation. |
| Python automatically inserting the origin into Agent 02 curve anchors | Keep its removal. A theoretical zero is not a measured marker. |
| New instruction to add one origin row per eligible series | Narrow it: a source curve reaching zero does not by itself prove an experimental marker at zero. Leave ambiguous ownership unresolved. |
| Removal of post-Agent-04 deleted-row demotion/restoration | Keep. Python must not recreate content after the final reviewer removed it. |
| Agent 04 source-based marker-style corrections | Keep for actual marker rows; exempt line samples from marker glyph inheritance. |
| Explicit auditing of evidence-type changes | Keep; restore matching authoring instructions. |
| Marker and trace count separation in Python summaries | Keep and extend it to stage summaries and review reports. |
| Confidence/rounding normalization | No evidence in this review that it causes the reported visual regression; do not roll it back with trace behavior. |
| Automatic best-round spec replacement | Replace with agent selection; retain numerical comparisons as diagnostics. |
| template_fill_unresolved=true | Prefer unresolved hypotheses in supporting material rather than fabricated candidate point rows. |

For crowded regions, give Agents 03/04 a native ROI with overlapping edge context; the source marker and line style; uncertain candidates with alternative owners; and a disposition ledger. Use “confirmed marker,” “partial marker,” “line only,” “duplicate,” “wrong series,” or “unresolved” per candidate/region. An omitted candidate must remain searchable in the native ROI. Scientific ordering and trajectories can guide that search but cannot establish a marker's existence. Agent 04 should reconcile marker additions, losses, and reclassifications separately from trace coverage. Python then applies the reviewer-authored result and verifies identities, coordinate transformations, bounds, and accounting.

Do not use point count or visual curve similarity alone as the quality metric. For a small manually reviewed set of crowded regions, compare marker precision/recall, native-center error, series ownership, and false markers on line-only spans. Report traces independently. More rows can be a worse extraction, as the 800-row example demonstrates.

Baseline limitations matter. No directory named f2b_01 was found in this workspace or matching tracked-run history. f2b and f2b_02 exist, and f2b has multiple reruns, replaced stage outputs, and multiple cited CSV variants per attempt. Its current final output is already affected by trace sampling. Do not reset to an arbitrary cited file or assume the current f2b directory is the intended good baseline. Freeze the intended source/candidate/final artifacts and their hashes before making a comparison. Save code revision plus dirty-diff fingerprint, prompts/contracts, agent versions, and the chosen input snapshot alongside future runs; agent version alone does not identify a locally changing prompt.

combined_01/fig2b has a Python stage but no completed Agent 03/04 points.csv. Its log records an Agent 03 DefaultAzureCredential failure after an Agent 02 failure. That panel needs a completed run before quality comparison; its current absence is not proof of Agent 04 deleting points. A rerun after fixing code would also need working authentication, but no authentication or cloud changes were attempted in this review.

Validation completed locally: 117 tests passed and one failed across 14 relevant test files covering dense bands/circles, trace counts, final CSV QA, reviewer edits, ownership, coverage, round selection, calibration, reconstruction, workflow contracts, and reruns. The failing test is tests/test_agent03_coverage.py::test_missing_strip_shape_rejects_thick_line_but_accepts_marker_on_line. A plain thick line is accepted as native_visible by missing_strips._native_shape_support. This helper is unchanged relative to HEAD and missing_strip_recovery is currently false, so it is not the demonstrated cause of the current run. Keep it disabled until the marker-on-line versus line-only distinction is fixed.

Some passing tests explicitly enforce the undesired policy: tests/test_qa_csv.py::test_line_sample_inherits_series_marker_and_rejects_generic_dot_style rejects marker_shape=none. Update that expectation as part of the selective rollback. Also, .gitignore currently excludes the entire tests/ directory and git ls-files reports no tracked copies of the inspected tests. Keep tests as tracked source and move/ignore only generated fixtures; otherwise a clean checkout will not retain these checks.

Suggested implementation order: freeze the intended baseline; reverse the coordinated trace-as-marker policy and restore Agent 04 evidence instructions; stop automatic structure rollback; fix the focused ROI and overlapping-hypothesis handoff; then compare the fixed pipeline against the frozen baseline using the same source pixels and manually reviewed crowded regions. Keep telemetry throughout. Re-run final CSV validation on regenerated artifacts, and keep any output failing that contract out of accepted publication.
