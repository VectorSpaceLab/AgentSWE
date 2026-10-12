# Construction report

## Inventory

- Public dev cases: 2.
- Hidden cases: 6.
- Uniform Candidate delivery: `solution.patch`, `edit_report.json`,
  `run_report.json`.
- Uniform lower artifact: `agent_result.json`, `trajectory.jsonl`,
  `launcher_result.json`, and broker delta evidence.

## Design decisions

- Kept the authoritative Edit objective and source pin.
- Replaced native-harness Result semantics with real DeepTutor lower-agent
  behavior.
- Reused protocol ideas, not Codex residual case content.
- Made dynamic identifiers and expected state evaluator-owned.
- Kept public cases descriptive; no answers, oracle values, or evaluator
  assertions are stored in a public case directory.
- Reserved cross-path/tamper/stale-head combinations for hidden coverage.

## Leakage and consistency checks

- Builder-visible roots are specified as exactly `input` and `dev_cases`.
- Hidden runtime definitions live under `evaluator/cases`.
- Candidate credential is always a placeholder; the broker credential file is
  never a lower mount.
- Result and Code rubrics total 100 independently and use disjoint evidence.
- No case requires reading the reference Candidate or reproducing its files.

## Deliberate Stage A limitations

- No real model call, Docker lower run, simple pilot, or formal run was started.
- No score is publishable.
- Broker/function-call compatibility and the prepared DeepTutor runtime remain
  to be validated in Stage B.
