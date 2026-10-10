# Requirements and Constraints

## Functional requirements

Interpret multi-hop factual questions; use search and page visits when configured; distinguish evidence from guesses; extract a succinct exact answer; degrade clearly when search or model access fails.

## Implementation constraints

- The initial `run_harness.py` and `agent/**` are provided and are the only editable paths.
- Do not add or modify files outside the `editable_space` declared in `task_contract.json`.
- Baseline and candidate use the same model, broker budgets, runtime and official evaluator.

- Run non-interactively through the uniform command.
- Support multiple JSONL rows and isolate per-row working state.
- Treat tool, model, network, VM, and simulator failures as explicit errors.
- Stay within the declared per-row timeout and provider/tool budgets.
- Write only to the designated output and run directories.
- Never access hidden cases, evaluator gold, reference trajectories, or other submissions.
- Do not request or emit private chain-of-thought; concise traces and usage counters are sufficient.
