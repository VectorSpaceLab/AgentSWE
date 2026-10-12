# Requirements and Constraints

## Functional requirements

Maintain conversation state; identify the user; use read and write tools with validated arguments; refuse unsafe or policy-violating requests; communicate required facts; treat equivalent database end states as correct rather than copying one reference trajectory.

## Implementation constraints

- The supplied `run_harness.py` and `policy_react.py` are the only editable paths.
- Do not add or modify files outside the `editable_space` declared in `task_contract.json`.
- The official simulator, domain policy, database, tools, user simulator, model/provider, budgets and reward are fixed for baseline and candidate.

- Run non-interactively through the uniform command.
- Support multiple JSONL rows and isolate per-row working state.
- Treat tool, model, network, VM, and simulator failures as explicit errors.
- Stay within the declared per-row timeout and provider/tool budgets.
- Write only to the designated output and run directories.
- Never access hidden cases, evaluator gold, reference trajectories, or other submissions.
- Do not request or emit private chain-of-thought; concise traces and usage counters are sufficient.
