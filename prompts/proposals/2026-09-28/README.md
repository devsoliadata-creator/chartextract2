# Proposed Agent 03 and Agent 04 instructions

These replacements were installed in the active local prompt files on 2026-09-28. Cloud prompt pins remain unchanged. No tests or workflow runs were performed for this installation. Deterministic bookkeeping reconciliation will flag a review when final CSV/report counts or change audit disagree, without changing coordinates.

The density/overlap policy now requires one row per source marker instance in crowded, high-density, and overlapping areas. A localized N-versus-N+1 ambiguity uses one additional estimated marker per cluster, preserved across both reviewers, with the competing counts and positional uncertainty recorded. Explicitly declared overlap instances survive proximity checks and may share native coordinates with separate IDs and provenance; Python does not add the extra row itself. When all source series appear to share a linear-axis (0,0) start and their first markers are unresolved, each affected series receives one estimated origin marker with an explicit assumption and uncertainty; this is not double-counted as another extra in the same crowded start. Each temporary reviewer version uses the current local workflow instructions so old pinned wording cannot override the new policies. No tests or extraction runs were performed for this update.

- [Agent 03 draft](agent03_review_points.md): independent native marker extraction, delivered through the existing edit-report interface.
- [Agent 04 draft](agent04_final_check.md): independent recovery, calibration verification/correction, and the final complete replacement CSV.

## What to preserve from the successful instructions

The most useful difference is their concrete measurement procedure. They tell an agent how to recover difficult points, rather than only telling it to review an upstream table. The reported successful outcome supports preserving that procedure, although it does not isolate which instruction caused the improvement.

| Successful instruction | Treatment in these drafts |
|---|---|
| Nearest-neighbor native crops and exact crop transforms | Retained, with detailed work focused on difficult regions rather than generating every possible view |
| Canonical templates from isolated in-plot markers | Retained; legend identity and in-plot geometry are checked together |
| Line thickness measured separately from marker size | Retained; a marker-plus-line component centroid cannot stand in for a marker center |
| Multiple explanations for merged components | Retained, adding a line-only/no-marker alternative; the 1.35 area ratio is a search cue, not a multiplicity rule |
| Logical instances survive shared x/native coordinates | Retained when each instance has source support; no global distance suppression or count-based duplication |
| Explicit inferred and unresolved decisions | Mapped to the current evidence enums and report fields |
| Whole-source completeness pass | Made central to both agents, including regions with no Python candidate, slot, crop, or trace |
| Separate markers and continuous-line samples | Retained, with authored segment/order and no fabricated connecting chords |

The drafts emphasize that absence of Python support is not absence of native evidence. Both agents create their own native crops and measurements. If segmentation fails, they can use visual inspection and manual geometric selection in Code Interpreter. A genuinely unresolved marker remains recorded as such; a smooth line is not silently promoted into an observed marker.

## Conflicts removed from the supplied prompt

The attachment combines overlapping prompt generations. Directly copying it would introduce incompatible requirements:

- It specifies two different fixed CSV headers (18 and 16 columns), while the current workflow requires the current 31-column core contract/example.
- It alternately requires five outputs, six outputs, workflow-defined outputs, and a ZIP. Agent 03 currently returns one JSON report; Agent 04 returns one CSV and one JSON report.
- It alternates between one selected panel and extracting all panels, and between excluding insets and independently extracting them. This workflow already receives one scoped panel and excludes insets.
- It both rejects line-only numeric extraction and later requires line tracing, including the unsupported `marker_shape=line_trace`. The current trace representation is `line_sample` with `marker_shape=none`.
- It permits unresolved CSV rows with blank coordinates. The final CSV loader requires finite x/y/native centers; unresolved regions belong in the report.
- It references a registry, `workflow_files.py`, and readiness fields that are not the input contract for these calls.
- Generic bar, heatmap, categorical-axis, multi-axis, panel-discovery, and source-recropping instructions distract from this CO2 marker task and exceed the current output interface.

The rewrite keeps the measurement methods while resolving these conflicts explicitly.

## Evidence mapping

| Legacy concept | Agent 03 operation evidence | Agent 04 CSV evidence | Display |
|---|---|---|---|
| Direct | `native_visible` | `visible_marker`, inferred=false | Normal series marker |
| Merged-supported with localized partial glyph | `partial_marker` | `partially_visible_marker`, inferred=false | Normal series marker |
| Inferred center of a supported instance | `estimated` | `estimated_marker`, inferred=true, low confidence, x/y uncertainties | Normal series marker; status in table |
| Unresolved center/owner/multiplicity | Coverage/uncertainty report, no invented position | `unresolved_slots`, no invented CSV row | Region remains unresolved |
| Native line trace | Describe for Agent 04; current operations cannot add traces | `line_sample`, marker=none, inferred=true, low confidence | Explicit segments without marker glyphs |

This mapping does not assert that every successful template fit is an observation. The agent must still distinguish a partial marker from fitted line ink and preserve uncertainty about ownership or multiplicity.

## Architectural limit and recommendation

The successful prompt gave Agent 03 ownership of candidate CSV creation and calibration. Today's Agent 03 schema accepts only operations against the host proposal; it cannot return replacement calibration, define new series, or author trace rows. These drafts are compatible with that interface: Agent 03 independently measures native centers and reports calibration conflicts, while Agent 04 can supply final calibration and a full CSV within the current spec constraints.

For a later architecture change, consider giving Agent 03 a full candidate CSV plus calibration/report interface analogous to Agent 04. Then Python would package evidence and validate/apply files, Agent 03 would own candidate extraction, and Agent 04 would independently finalize it. That would require schema, host, audit, and handoff changes; changing a prompt alone cannot provide this capability. It is not implemented by this proposal.

## Azure skill usage and scope

Guidance consulted: the installed `microsoft-foundry/SKILL.md`, its `foundry-agent/observe/observe.md` workflow, and `foundry-agent/observe/references/optimize-deploy.md`. The skill's dependency check completed with azd and the microsoft.foundry extension available.

The guided cloud optimization route requires the `prompt_optimize` MCP operation, which is not exposed in this session. These files are therefore manual proposals based on the supplied successful prompt, failure analysis, and current runtime contracts. They are not outputs of Azure Prompt Optimizer, and no comparative quality improvement is claimed. No tests, agent invocations, evaluations, or deployments were performed for this rewrite.

The installed copies in `prompts/agent03_review_points.md` and `prompts/agent04_final_check.md` are loaded into subsequent reviewer requests; the proposal-directory copies are retained for reference. Keep the source/template recovery and output-contract sections together. These changes use local prompt files and the locally supplied Agent 04 report schema, so no cloud version/pin change was needed.
