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
