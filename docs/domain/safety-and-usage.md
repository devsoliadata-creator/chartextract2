# Safety and usage

- **Structured output only.** Every agent step includes its schema in the local prompt and the answer is
  validated again with `jsonschema` before use. An invalid answer fails only that panel.
- **Models never write data values.** Agents 03 and 04 return operations on point ids and normalized source positions;
  Python converts them through the calibrated axes and records every operation (the `edit_audit` section of `<stage>/audit.json`), including
  operations it refused (unknown point, outside the plot frame, excluded series).
- **Limits** live in `workflow.yaml`: `timeout_seconds`, `max_image_side`; agent 03 attaches at
  most 12 images.
- **Content safety** is a platform control: attach the approved guardrail policy to the Prompt Agent/model in the
  Foundry portal (**Build → Guardrails**). Don't put policy ids in source code.
- **Secrets**: keep `.env` private and out of any copy of the code you hand over; your `az login` identity (`DefaultAzureCredential`) authenticates the model
  calls, so no keys are stored.
- Treat model output as untrusted; rows in `all_data_review.csv` need human review before analysis.
- **Estimated points.** Agents 03/04 may add or move a point without a visible marker centre only when the
  series' own ink is present (a fused crowded-strip stripe, an occluded marker) and other evidence (shared
  column, the series' own trajectory, temperature ordering) supports the position (`evidence_kind`/`evidence_type`
  `estimated`/`estimated_marker`, `docs/naming.md` vocabulary; `confidence=low`, mandatory `uncertainty_x/y`).
  Such rows are always `is_inferred=true`, are never compatible with `verdict=accept`, and always land in
  `all_data_review.csv`, never `all_data_accepted.csv`.
