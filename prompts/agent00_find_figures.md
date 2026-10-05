# Agent 00: Qualify a figure candidate

## Objective

Screen one figure candidate before any extraction happens. Decide which of its panels are CO2 adsorption
isotherms and collect the experimental context. The coordinates you return are only rough crops; Python
detects the axes precisely afterwards.

## Inputs

This is candidate `$candidate_id` from a research paper. The following image inputs were selected by
`runtime/agent00.yaml`; `figure.png` is the extracted figure and `page.png`, when present, is its complete source page:

$runtime_files

Document-wide discovery context (title and every caption in the document):

$document_context

Caption candidates on this page:

$caption

Sentences in the paper that reference this figure:

$references

## Instructions

First match the extracted candidate to the correct caption visible on its page. Return the exact printed
figure number and caption. If it is an unnumbered graphic, use null and an empty caption; never assign
captions by image order alone.

Apply this extraction policy as the authoritative eligibility rule:

```json
$co2_scope
```

Exact values reported in prose, tables or captions are not chart tasks: do not extract or estimate numeric
values here, and do not mark a figure relevant only because its caption quotes a number.

- Ignore inset charts (a small chart embedded inside a larger one) but report their location so they can be masked.
- If a panel plots CO2 together with other gases (N2, CH4, ...), still include it; only CO2 series will be extracted.
- Use the caption and the referencing sentences to recover parameters (temperature, adsorbent, activation, units, pressure range).
- A panel means one distinct physical chart/axis frame. Caption letters, legend entries, gases, temperatures,
  adsorption/desorption states and series labels do not create panels by themselves.
- If the figure contains exactly one physical axis frame, return exactly one panel named `x`, even when the
  caption labels individual series `(a)` through `(h)`.
- Never return duplicate or substantially overlapping panel boxes for the same axis frame.
- `bbox_norm` is the whole panel including axis labels and legend, as fractions 0-1 of the full image
  (`[x0, y0, x1, y1]`, origin at top left). `inset_bbox_norm`, when present, uses the same coordinates.
- A legend outside the axis frame is still part of the panel. Extend the panel box far enough to include every
  legend row, marker, and label; do not stop the right edge at the plot frame or at the first visible legend row.
- Check all four proposed crop edges against the source image. If an edge would cut through legend text or symbols,
  move it outward to include the complete legend while keeping neighboring panels separate.

## Response format

Also say whether the paper mentions supplementary / SI tables with the isotherm data (then digitising is
unnecessary). Fill every field. This example shows the response shape only; use the actual source evidence
for every value:

```json
$example
```
