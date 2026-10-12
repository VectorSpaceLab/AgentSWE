# Evaluator instructions

Cycle-10 additionally probes the read-only `--integrity --cross-instance-seal`
view. Missing or failed cross-instance lineage proof triggers the existing
integrity bypass cap while preserving case validity; it must never be reported
as provider, Docker, or evaluator infrastructure failure.

Use `evaluator/harness/evaluate_suite.py` as the authoritative evaluator. It
receives the pristine pinned repository, the candidate patch/reports, the
evaluator-owned `test_cases/`, and an empty output directory. Never expose
hidden material to the builder. Treat all candidate output/state as
untrusted.

The suite initializes evaluator-local Git metadata, applies the patch
exactly once, proves that a second application is refused, validates the
reports/paths, preserves the existing plugin hashes, installs a copy,
launches the two pinned entries, and measures the actual process-tree PSS.
It never invokes the Claude/provider core, uses network access, decodes
candidate-private state, or applies virtual-memory pressure.

Run all six isolated cases after input validity. Convert a post-input
timeout, crash, malformed protocol, or oversized response into a bounded
case-local zero and continue. Patch/report/build/entry/resource, missing or
malformed results, assertion/cap identity drift, score accounting errors,
or other evaluator integrity failures invalidate the suite.

Validate `evaluator/assertion_contract.json` before aggregation: exactly 6
ordered cases, 100 assertion points per case, exact runtime IDs/weights,
and only locally confirmed caps declared. Score only final behavior. Never
grant credit for candidate tests, reports, symbols, implementation style,
private encodings, or claims.

Repair evidence is valid only through the generic receipt fields, the exact
public manifest/status/attestation/commit exits, the deliberately corrupted
repair logs, replay/crash outcomes, `--repairs`, and the repaired audit
chain. Never decode staging/quarantine files or grant credit merely for
emitting the obvious. A corrupted suffix cannot be accepted as a new append
anchor.

Continuation evidence is valid only through the exact token issuance
response, source/target hook results, exact cancel/reconcile exits, session
authority fields, `--continuations`, and the correlated old-machine view.
Never decode tokens, infer activation from issuance, or treat session
continuation as ownership handoff. The bearer may appear only in its
successful issuance response and the reserved target hook input.

Approval, reservation, checkpoint, ownership, upgrade, and effect evidence
stays valid only through their documented receipts, exact envelopes/views,
process exits, and audit events. Managed checkpoint import remains
`checkpoint_only`; no repair or continuation outcome may let two owners be
live at the same time.

The required `run_summary.json` contains suite validity, patch/report/entry
proofs, per-case scores/raw scores, assertion evidence, triggered caps,
bounded commands/durations/PSS, all six scores, and the arithmetic mean.
Evaluation fixture secrets and corruption payload markers must not appear
in reports or the console.

Cycle-9 must also run the independent integrity sub-scenario of each case:
read that case's `policy_integrity.json`, verify the first seal and the
exact `--integrity` view, check the refusal of protected Write and Bash
targets under `./`/duplicate separators, then, after one seal, have the
evaluator harmlessly modify the hook file bytes and verify the durable
`compromised`/quarantine and the fail-closed after restoring the original
bytes. Score only from the final responses, views, exit codes, and state
evidence; do not decode the candidate's private seal files or rely on
filename oracles.
