# 0905 Edit repair migration note — Codex execution residual

Date: 2026-09-05

This sibling remains `REPAIR`; no paid provider call, Docker workload, or formal
hidden run was started during the repair.

The formal runner now accepts up to ten distinct valid submissions, evaluates
both dev cases for each accepted digest, does not freeze automatically on
`dev mean > 60`, and runs the complete `test_001` through `test_006` inventory
after freeze. The lower run continues to execute the frozen patched Codex
binary and binds the model-authored artifact to native rollout evidence. Result
uses the shared independent semantic judge and Code routes through the
evaluator-owned wrapper around the unchanged Create judge.

The September 5 static repair also makes the one-stop refuse a non-empty
`run-dir`, places every broker startup attempt inside the cleanup `try/finally`,
and records per-role cleanup/absence evidence. The lower harness labels its
deterministic score as a non-publishable native evaluator measurement, records
the copied `workspace/agent_result.json` digest and authorship provenance, and
separates provider/broker/evaluator failures from Candidate contract failures.
The formal finalizer rejects missing, synthesized, case-mismatched, or
digest-mismatched artifacts before invoking the shared Result judge.

Provider-free verification completed on September 5, 2026:

- `PYTHONPYCACHEPREFIX=/tmp/0905-codex-pycache python3 evaluator/harness/self_test_formal.py`
  returned `AGENTLOOP_FORMAL_SELF_TEST=PASS`.
- `PYTHONPYCACHEPREFIX=/tmp/0905-codex-pycache python3 -m compileall -q evaluator harbor`
  completed successfully.
- `PYTHONPYCACHEPREFIX=/tmp/0905-codex-pycache python3 -m unittest -v evaluator/test_0905_contracts.py`
  covers run-directory preservation, startup-cleanup bookkeeping, fixed shared
  judge entries, artifact authorship/digest binding, and provider/Candidate
  failure separation without provider or Docker execution.

`evaluator/harness/self_test_formal.py` is root-owned and not writable by this
repair process, so the new provider-free regression file carries the added
September 5 assertions without changing permissions or replacing that test.

These checks do not claim a lower-agent or judge success. A new real hidden
smoke with broker usage, independent Result-judge usage, model-authored
artifact provenance, and cleanup attestation is still required before
readiness can become `READY`.

## September 6, 2026 evidence-chain tightening

The independent Code path now uses a sibling-local eight-dimension Code rubric,
separate from the behavioral Result rubric. Hidden execution records measured
the frozen Candidate digest before and after each case and stops fail-closed on
mutation; the finalizer requires both measured digests to match the immutable
freeze manifest. Lower artifact provenance additionally records pre-launch
absence, successful lower-broker use, and a current-trajectory reference, while
Docker exit-125 launcher failures and evaluator-enforced timeouts remain
infrastructure classifications.

These are provider-free contract repairs only. No current six-hidden smoke or
formal score is claimed.
