# DeepTutor Agent-loop probe report

## Scope and stop point

This report records the state after the DeepTutor-only harness work was
stopped at the user's request. The only write target was this sibling:

`18-edit-deeptutor-adaptive-remediation-v5-agentloop-v1`

No authoritative product source, 0830 staging tree, old formal run, or global
manifest/progress file was modified. The long-running clean hidden rerun was
not started; the temporary broker processes created by this probe were
stopped.

## Sibling files changed

- `agentloop/two_round_controller.py`
  - Requires both public dev terminal evaluator records for each consuming
    submission.
  - Carries evaluator feedback after Candidate 1 and requires feedback for
    Candidate 2.
  - Enforces one Builder session ID and a different Candidate 2 digest.
  - Copies Candidate 2 into `frozen_candidate_2`, verifies its digest, and
    makes the snapshot read-only for ordinary processes.
  - Refuses to emit a fake `not_run` hidden result; hidden execution must use
    `agentloop.run_hidden`.
- `agentloop/run_hidden.py`
  - Requires a verified Candidate 2 freeze before hidden execution.
  - Runs exactly `test_001` through `test_006` sequentially.
  - Gives every case a fresh writable copy of the frozen Candidate and a fresh
    output/runtime directory.
  - Records broker before/after statistics and a per-case delta, launcher
    result, terminal artifact, process output, and frozen digest checks.
  - Writes `hidden-after-freeze-attestation.json` and never writes a Result or
    Code score.
  - Treats broker budget exhaustion as `broker_infrastructure_error`; this
    guard was added after the first probe exposed a shared-suite budget edge.
- `README.md`
  - Documents the explicit post-freeze hidden command and failure boundary.
- `tests/test_agentloop_runtime.py`
  - New dependency-free `unittest` coverage for freeze/feedback/session gates,
    immutable snapshot behavior, six-case hidden invocation/attestation, and
    pre-freeze rejection.
- `meta/agentloop_probe_report_20260902.md`
  - This evidence record.

The `.pilot-runtime-final-evidence/` directory contains generated probe
evidence inside this sibling. It is not a product source tree or a formal run.

## Tests and static checks

Using the task-local interpreter
`@@AGENTSWE_ENVS@@/deeptutor-task-env-v1/venv/bin/python`
(`Python 3.12.13`):

```text
python -m unittest -v tests/test_agentloop_runtime.py  PASS (3/3)
python agentloop/self_test.py                              PASS
python -m compileall -q agentloop tests                    PASS
```

The system Python in this workspace is Python 3.10.12 and lacks the DeepTutor
runtime dependencies. The task-local venv has the required runtime modules and
`python -m deeptutor_cli --help` exited 0. It does not contain `pytest`, so
the new tests intentionally use only the standard library.

## Real public-dev evidence

The real lower entry was the production CLI path:

```text
python -m deeptutor_cli run mastery_path <prompt> --format json
→ MasteryPathCapability.run → AgenticChatPipeline → DeepTutor AgentLoop
```

Evidence is under `.pilot-runtime-final-evidence/`:

- Candidate 1 `dev_001`: `agent_result.json`, `trajectory.jsonl`, and
  broker before/after evidence exist; process exit 0; 9 successful broker
  calls; 65,000 tokens; no provider failures.
- Candidate 1 `dev_002`: terminal artifact and trajectory exist; 3-call
  broker delta; cumulative broker total 84,261 tokens; no provider failures.
- Candidate 2 `dev_001`: terminal artifact and trajectory exist; 9 successful
  broker calls; 65,627 tokens; no provider failures.
- Candidate 2 `dev_002`: terminal artifact and trajectory exist; 2 successful
  broker calls; 12,599-token delta; no provider failures.

All four were real product executions with the locked
`gpt-5.6-sol` / `medium` broker contract. Each was classified
`candidate_capability_gap`, not infrastructure failure: the Candidate exposed
the requested adaptive surfaces but the public tool audit still found
`mastery_learner_snapshot_chain_audit` missing. No score was assigned.

The Candidate 1/Candidate 2 lifecycle evidence is a harness probe, not proof
of a real Builder conversation: Candidate 2 was constructed from the
materialized Candidate 1 tree with a revision marker after recording
evaluator-style feedback. It proves the controller/freeze mechanics, not
same-session Builder behavior.

## Real hidden evidence and why it is not formal

The first post-freeze run is:

`.pilot-runtime-final-evidence/hidden/hidden-after-freeze-attestation.json`

It proves that the runner invoked all six hidden cases after a Candidate 2
freeze, used fresh case copies, and kept the frozen digest stable. The case
directories contain real DeepTutor `agent_result.json`/`trajectory.jsonl`
artifacts for `test_001` through `test_005`; `test_006` was invoked after
the broker budget had already been exhausted. The observed broker state was:

```text
31 calls
31 successful calls
0 provider failures
207,407 total tokens
budget_exceeded = true (200,000-token suite limit)
```

Therefore the attestation says `all_cases_invoked=true` and
`frozen_candidate_stable=true`, but `formal_result_eligible=false`. This is
real hidden execution evidence and a useful failure-attribution probe, not a
formal hidden score. The corrected budget guard was added afterward.

A new broker with a larger suite ceiling was started for a clean rerun but was
stopped before any lower-agent case was launched when the user requested an
immediate halt. There is no clean six-hidden rerun to report.

## Current blocker list

1. The product capability surface still lacks
   `mastery_learner_snapshot_chain_audit`; this must remain a candidate
   capability gap unless a future Builder Candidate legitimately adds it.
2. Same-Builder-session Candidate 2 is not formally proven. The controller
   enforces the invariant, but the recorded Candidate 2 is a harness-generated
   revision rather than a real feedback response from a Builder session.
3. The completed hidden probe crossed its 200,000-token broker suite limit,
   so at least one case is broker-budget infrastructure failure and the suite
   cannot be used for formal Result aggregation.
4. A clean post-freeze six-case rerun with the corrected budget attribution
   guard remains required. It must preserve per-case broker deltas, fresh
   runtimes, and attestation.
5. Formal Agent-loop Result, independent Code score, and aggregation remain
   `N/A`; no score has been fabricated or published.

## Current conclusion

DeepTutor now has a substantially more complete fail-closed harness and has
real public and partial hidden execution evidence. It is not yet formally
ready: the next run must be a clean six-hidden attribution probe, and the
same-Builder-session Candidate 2 condition must be established before any
formal Result/Code judgment.
