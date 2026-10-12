# Reference Baseline

## v3 status

The Search ReAct starter is runnable and digest-locked, but its full 50-row official development baseline has not yet run. No numeric baseline or improvement is claimed. A formal one-stop run writes evaluator-owned `baseline.json` and evaluates baseline/candidate through identical resources.

The pinned reference source was inspected locally. A full paid-model/VM run was not claimed during case construction. The source provides the native task format and evaluator contract; this benchmark intentionally uses a small frozen split and a uniform `run_harness.py` wrapper. A no-model protocol smoke is expected to produce malformed/empty predictions and is scored as an execution or ordinary low-quality result, never as a hidden gold answer.

The reference implementation was inspected; model and browsing credentials were not used during construction.
