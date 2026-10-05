# Regenerated Python stage with the dense ribbon resolver (2026-10-05)

Four saved panels re-extracted locally with `extract.extract(spec)` on the final `spec.json`
of each run, then written through `write_python_stage` (so rows carry the on-ink flag and
the resolver's additions). Baseline "old Python" is the saved `python/points.csv`, which was
Agent 02's per-series best-round composite, so small per-series differences are expected
even without the resolver.

| panel | old Python | old Agent 03 | new Python | ribbon added (measured + estimated) | rows flagged off-ink |
|---|---|---|---|---|---|
| combined_05/fig2a | 374 | 388 | 415 | 41 | 38 |
| combined_04/fig2a | 347 | 364 | 378 | 46 | 34 |
| combined_04/fig3a | 59 | 66 | 60 | 1 | 1 |
| combined_03/fig2b | 117 | 112 | 121 | 14 | 2 |

Per-series detail: `regeneration_summary.json` (sent with the overlays).

## What improved

- The fused low-pressure bands of 273K and 283K on both fig2a runs now carry a train of
  resolver rows (about 10 to 12 per band), measured on the ribbon, each with a native citation
  and a pixel uncertainty. Before, those bands had almost nothing.
- 293K, 313K, 323K, 373K gain 3 to 6 measured centres each in the fan.
- Rows that do not sit on their own series' colour are now visible to the reviewers
  (`on_series_ink=false`): 34 to 38 per fig2a run, concentrated in 273K and 303K, which the
  4x crop earlier showed to be other series' glyphs mis-assigned.
- The hollow-marker false positives on fig3a are gone after self-calibrating the on-ink
  reference per series (18 flagged rows became 1).

## Known gaps after this pass

- combined_03/fig2b 303K (red fused band): the tracker does not follow the band
  (ink-mass count 1.8); the series ends with 14 rows against 22 before. The green 323K band is
  similar. Both bands are overlaid by other series' glyphs, which breaks the colour ribbon.
  Next step: track on a combined "series colour OR dark line" mask, or seed from the
  Agent 02 `y_bands` instead of relying on colour continuity alone.
- combined_04/fig3a vertical rise (60+ stacked open circles): count-only, 1 estimate placed
  at the knee. Needs the ink-mass count to be allowed to place along a vertical stretch with
  the y-setpoints rather than x-setpoints.
- Fully fused black bands remain count-bounded, not centre-resolved; the estimates are
  explicitly `estimated_marker`, low confidence, for the reviewers.

## Second pass: mis-assigned rows pruned (same day)

| panel | new Python rows | pruned as mis-assigned | still flagged off-ink |
|---|---|---|---|
| combined_05/fig2a | 385 | 30 (273K 12, 303K 12, 293K 3, others 3) | 9 |
| combined_04/fig2a | 349 | 29 (273K 13, 303K 8, 283K 3, 293K 3, 373K 2) | 5 |
| combined_04/fig3a | 60 | 0 | 1 |
| combined_03/fig2b | 119 | 2 | 0 |

The clump of black 273K squares on neighbouring curves is gone from the recreation.

## Third pass: hue-aware ink mask (same day)

Why the red 303K and green 323K bands on combined_03/fig2b were missed: the fused bands are
drawn a darker shade of the series colour (lightness 84 vs 132 for red, 92 vs 130 for green),
so the full-Lab distance (41 to 58 units) failed the 34-unit mask and the tracker stopped after
one marker. The mask now also accepts same-hue shades (hue angle within 18 degrees, chroma at
least 40% of the marker's); grey and black series keep the full Lab test.

| panel | new Python rows | ribbon added | notes |
|---|---|---|---|
| combined_03/fig2b | 150 (was 117) | 45 | 303K 22 -> 31, 323K 19 -> 32, 373K 18 -> 30: all three bands traced to the origin |
| combined_05/fig2a | 381 | 39 | off-ink flags fall to 3 because shades now count as the series' ink |
| combined_04/fig2a | 338 | 37 | a few series lose 2 to 5 rows to pruning; see the audit before trusting them |
| combined_04/fig3a | 60 | 1 | unchanged |
