# Dense-region extraction: prototype results (2026-10-05)

Prototype: `scripts/prototypes/dense_ribbon_resolver.py`, run on `combined_05/fig2a`
(11 CO2 series, raster figure, dense fan below ~150 mbar).

## Method

1. Measure each series' real colour at its clean markers (right of 300 mbar), build a
   colour mask with a tolerance floor of 34 Lab units so the anti-aliased ribbon is included.
2. Marker radius and ink area from the radial profile of those clean markers; connecting-line
   width from the ink between them.
3. Track the series ribbon leftward from the leftmost clean marker by arc-centroid stepping,
   re-centred on the ribbon cross-section each step. Rules: never head rightward (an isotherm
   is single-valued in x), bridge gaps up to one marker spacing, stop at the zero line.
   Existing Agent 03 rows are used as waypoints only when they sit on their own series' ink.
4. Ink profile along the path; local peaks above the line level are marker centres
   (`partially_visible_marker`, measured).
5. Ink-mass count along the path gives a lower bound on markers; the shortfall against
   peaks + existing rows is placed at visible pressure-setpoint columns on the path
   (`estimated_marker`), else evenly along the fused stretch.

## Result on combined_05/fig2a (dense region, x < 300 mbar)

| series | existing rows on ink | existing rows OFF ink | new measured peaks | new estimates |
|---|---|---|---|---|
| 273K | 4 | 14 | 5 | 2 |
| 283K | 9 | 3 | 5 | 1 |
| 293K | 5 | 6 | 6 | 0 |
| 303K | 4 | 12 | 1 | 0 |
| 313K | 6 | 4 | 3 | 0 |
| 323K | 8 | 3 | 5 | 0 |
| 333K | 10 | 2 | 2 | 0 |
| 343K | 10 | 2 | 2 | 0 |
| 353K | 9 | 0 | 1 | 0 |
| 363K | 7 | 2 | 0 | 0 |
| 373K | 6 | 5 | 4 | 0 |

- 34 new measured markers and 3 estimates, all on real glyphs (see overlay).
- 53 existing Agent 03 rows in the dense region do not sit on their own series' colour.
  Most are visibly other series' markers mis-assigned (black 273K rows on teal/purple glyphs).
  This is a precision problem in the current Python/Agent 03 table, found for free by the
  on-ink check.

## Limits found

- The 273K and 283K bands below ~50 mbar are solid ribbons at native resolution: markers
  overlap by more than their width. Ink mass gives a lower bound only (273K: ~11 markers
  over the whole 0-300 mbar stretch, which is certainly an undercount). No pixel method can
  place individual centres there; the honest output is the curve plus count-based estimates.
- Same-colour series (333K/373K navy, CO2 vs N2 pairs) are kept apart only by tracking
  continuity; where they merge at the origin the tracker must stop.
- Marker glyph shape is not yet used (template correlation); peaks are ink-profile only.

## Next steps to wire it in

1. Add the on-ink check to `stage_artifacts`/`hard_checks` as a per-row diagnostic
   (`on_series_ink=false`) so Agents 03/04 see the suspects.
2. Replace `dense_regions` resolver gating (needs 2 confident owners per column) with this
   ribbon tracker; emit peaks as candidates and estimates with uncertainty.
3. Use the setpoint columns from Agent 01 (`x_setpoints_visible`) as the primary placement
   for fused stretches, with the ink-mass count as the cap.
4. Template correlation along the path for shape-aware peaks (improves same-colour cases).
