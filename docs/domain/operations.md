# Operations

## Calls per document

Agent 00 makes one call per figure candidate. Each eligible panel then makes one agent 01 call, one agent 02 call per
check round (1 to `max_check_rounds`), and one call each for agents 03 and 04. Calls run one after another; there are no batch calls.

## What happened in a run

Everything is on disk, per step, in `artifacts/runs/<document>/`:

- `discovery/run_summary.json` - every panel with its status, message, check rounds and row counts per stage;
- `<figure>/agentNN/answer.json` - each agent's answer (`agent02/round<N>.json` per check round);
- `<figure>/python/summary.json`, `<stage>/audit.json`, `qa.json` - Python's numbers, the hard checks (in `audit.json`) and the final QA;
- `<figure>/log.json` - one entry per model call / attempt: agent name/version, response id, usage, status, timestamp;
- the console log - one line per step and panel while the run is going.

## Configuration

| File | Holds |
|---|---|
| `workflow.yaml` | Prompt Agent behavior, local instructions, timeout, image size, edges |
| `config.yaml` | folders, naming, PDF and extraction tuning |
| `.env` | endpoint and Prompt Agent names/versions - keep it private |

## Failures

| Symptom | Where to look / what to do |
|---|---|
| 401 / 403 | `az login`; your login needs the **Azure AI User** role on the Foundry project |
| authentication mode errors | check `FOUNDRY_AUTH_MODE` (`entra` or `api_key`) and the matching credential variables in `.env` |
| `set FOUNDRY_AGENT_NN_NAME or FOUNDRY_AGENT_NN_VERSION` | set the Prompt Agent override in `.env` |
| change a Prompt Agent model | create a new version with the selected model in Microsoft Foundry, then pin its version in `.env` |
| `answer does not match schemas/...` | the model returned invalid JSON for that step; the panel is marked failed, see `error.txt` |
| panel `failed` after the check rounds | `python/error.txt`, `python/images/tick_check.png`, `agent02/round<N>.json` |
| panel `review` with "not satisfied after N rounds" | read `issues` in the last `agent02/round<N>.json` |
| panel `review` after agent 01 | agent 01 found no eligible CO2 series; check `agent01/answer.json` |
| any panel `failed` | `artifacts/runs/<document>/<figure>/error.txt` has the traceback |
