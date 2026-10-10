# BrowseComp Search Agent Optimization

## v3 optimization protocol

The Builder receives a runnable Clean Basic Search ReAct starter rather than an empty submission. Public development is 50 Arena validation rows and hidden evaluation is 100 Arena test rows. See `task_contract.json` for editable paths, frozen resources and metrics.

This case measures whether an agent builder can improve a reusable search harness rather than solve one question. Three public BrowseComp rows demonstrate the contract; five held-out rows are evaluator-owned. Provide the four `input/` documents to the builder, run only `dev_cases/` during optimization, then freeze the harness before running `test_cases/`. The evaluator compares the final exact answer with the pinned BrowseComp gold and records evidence, usage, and failures.
