# Claude policy-provenance Agent-loop sibling (Stage A)

This directory is a writable sibling of the read-only authoritative source
`11-edit-claude-policy-provenance-v3`.  It preserves the source benchmark and
adds an auditable Agent-loop migration layer under `agentloop/`.

The target lower product is the Candidate's own Claude Code policy plugin:
`plugins/policy-provenance-ledger/hooks/policy_hook.py` and
`plugins/policy-provenance-ledger/bin/policy-ledger-inspect`.  The launcher
does not run an external general-purpose Codex.  It asks the evaluator-owned
broker for a bounded plan/adjudication and executes the edited hook/inspector,
keeping model text and target-product trajectory separate.

The locked lifecycle is Candidate 1, both public dev cases, feedback, Candidate
2 with a different digest, both dev cases again, freeze, and only then hidden.
The broker forces `gpt-5.6-sol` with reasoning `medium`; the Candidate sees only
`broker-only-placeholder`.  Stats record calls, failures, and token counters.

`evaluator/rubric.md` and the copied native harness remain legacy/mechanism
references.  The new Agent-loop Result rubric is in
`agentloop/evaluator/result_rubric.py`; the independent fixed-weight Code axis
is in `agentloop/evaluator/code_rubric.py` and `code_runner.py`.

## Smoke boundary

The commands below are static/self-test and syntax/build smoke.  They do not
constitute a formal Result and do not prove real lower-agent behavior.  A
simple pilot must separately observe at least one successful broker call and a
hidden case after freeze.  Until those calls exist, the status is PARTIAL.

The copied source's native `evaluate_suite.py --self-test` remains useful for
the original mechanism contract, but it is not the Agent-loop Result.

`agentloop/evaluator/public_package.py` is the Builder-facing physical staging
adapter.  `dynamic_case_service.py` creates a per-run nonce and stores the
oracle outside the Candidate-visible payload; it is intentionally not invoked
by the Stage A smoke commands.
