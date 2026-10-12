# Stateful Multi-Application GUI Workflow Benchmark

This Create-Agent benchmark measures whether a coding agent can build a general local-browser automation agent that observes and completes realistic, stateful workflows. The cases require navigation, visual interpretation, state tracking, error recovery, precise scoping, review-before-commit behavior, and proof that the application itself reached the requested state.

## Builder Package

Give the builder all four documents in `input/`, in numeric order, plus `dev_cases/`. Do not reveal `test_cases/`, `evaluator/`, or `meta/` while the agent is being built. The builder creates `run_agent.py` and its dependencies in a submission directory and configures the dedicated Conda prefix named in `input/04_resources.md`.

## Fixture Launch

Every case is a self-contained local web application. Before invoking the submitted agent, the harness chooses an unused loopback port and a fresh high-entropy evaluator token, then starts:

```bash
python <case_dir>/assets/serve.py \
  --host 127.0.0.1 \
  --port <unused_port> \
  --evaluator-token <secret_token>
```

The server prints one JSON readiness line containing `url` and `case_id`. The harness sets `GUI_FIXTURE_URL` to that URL for the agent process but does not expose the evaluator token. Wait for `GET /health` to return `{"ready": true}`. Use a fresh fixture process, browser profile, output directory, and token for every run.

## Development Runs

Activate the dedicated prefix, load `/opt/agentswe/benchmark/envs/.env` without printing it, start the development fixture as above, and run:

```bash
GUI_FIXTURE_URL=http://127.0.0.1:<port> \
python run_agent.py \
  --input /absolute/path/to/dev_cases/dev_001/input.md \
  --output /absolute/path/to/run_outputs/dev_001
```

Run both development cases. The submitted agent must not start, stop, inspect, or modify the fixture process; it receives only the loopback URL.

## Hidden Isolation

Mount exactly one hidden case at a time after development. Do not expose sibling hidden cases, evaluator files, metadata, prior outputs, or the evaluator token. Enforce 600 seconds and 4 GiB per case. Permit the declared model/search endpoints, public retrieval allowed by the resource policy, and loopback access to the active fixture only.

## Application-Owned Evaluation

After the run, while the fixture remains alive, the harness requests `GET /__evaluator__/state` with header `X-Evaluator-Token: <secret_token>`. This returns the authoritative application-owned `state`, full UI event `trace`, decisive event snapshots, request count, and challenge metadata suitable for validation. The token must never be passed to the submitted agent or Eval Codex.

Validate all required JSON and PNG files. Inspect `initial_state.png`, `decisive_step.png`, and `final_state.png`; compare the submitted `automation_result.json` with the protected fixture record; and retain the bridge response as harness evidence. The decisive screenshot must visibly capture the review, conflict, validation, scoped selection, or pre-confirmation boundary immediately before the final state-changing action.

For each hidden case, provide Eval Codex the case input/assets, submitted final artifacts, screenshot renders, sanitized protected-bridge evidence, `evaluator/rubric.md`, and `evaluator/eval_prompt.md`. Never provide submission source code. Score each test independently and report the arithmetic mean of the six totals to one decimal place; an execution-failure case contributes zero.
