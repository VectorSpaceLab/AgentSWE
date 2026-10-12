# Requirements and Constraints

## Functional requirements

Read the task instruction; plan and execute shell commands; inspect files and tests; recover from failures; preserve the task workspace boundary; report usage and failures without claiming success before the official validator.

## Implementation constraints

- The supplied `run_harness.py` and `agent_policy.py` are the only editable paths.
- Do not add or modify files outside the `editable_space` declared in `task_contract.json`.
- Baseline and candidate use the same live Terminus-2 runtime, model, budget and official task tests.

- Run non-interactively through the uniform command.
- Support multiple JSONL rows and isolate per-row working state.
- Treat tool, model, network, VM, and simulator failures as explicit errors.
- Stay within the declared per-row timeout and provider/tool budgets.
- Write only to the designated output and run directories.
- Never access hidden cases, evaluator gold, reference trajectories, or other submissions.
- Do not request or emit private chain-of-thought; concise traces and usage counters are sufficient.
