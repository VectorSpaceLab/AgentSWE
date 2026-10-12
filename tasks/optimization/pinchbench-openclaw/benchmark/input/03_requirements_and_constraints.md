# Requirements and Constraints

## Functional requirements

Interpret the request, inspect available files, choose appropriate tools, create or edit the requested artifact, verify obvious correctness, and report failures honestly. Keep per-task state isolated and support multiple sequential rows.

## Implementation constraints

- The supplied `run_harness.py` and `productivity_policy.py` are the only editable paths.
- Do not add or modify files outside the `editable_space` declared in `task_contract.json`.
- OpenClaw, model/provider, tools, fixtures, timeout, transcript and `grade_task()` are fixed for baseline and candidate.

- Run non-interactively through the uniform command.
- Support multiple JSONL rows and isolate per-row working state.
- Treat tool, model, network, VM, and simulator failures as explicit errors.
- Stay within the declared per-row timeout and provider/tool budgets.
- Write only to the designated output and run directories.
- Never access hidden cases, evaluator gold, reference trajectories, or other submissions.
- Do not request or emit private chain-of-thought; concise traces and usage counters are sufficient.
