# Claude Code policy-provenance ledger Edit benchmark

This benchmark evaluates whether a coding agent can add a deterministic policy and provenance plugin at the public plugin/hook boundary of Claude Code. It does not edit the proprietary Claude core, launch a real `claude` binary, contact a provider, or require execution beyond a PreToolUse hook decision.

## Builder package

Provide only the Builder `input/` (including the fixed `input/repository/`) and `dev_cases/`. Do not provide `test_cases/`, `evaluator/`, or `meta/`. The Builder returns `solution.patch`, `edit_report.json`, and `run_report.json` in a delivery directory.

The frozen contract fixes the plugin root, manifest, hook configuration, Python hook path, stdin/stdout JSON, policy paths, state overlays, active and compact logs, receipt snapshot/key fields, and audit inspector. Private equivalent names are invalid. Cycle-2 receipts expose policy ID/epoch and optional delegation provenance. The policy preserves schema-version-1 compatibility while schema version 2 adds monotonic lineage and a crash-recoverable hash chain.

The optional reservation policy adds durable in-flight leases, atomic global/session quotas, idempotent post-settlement, crash expiry/reconciliation, and bounded edit-lease views without changing unlimited legacy behavior. Cycle 5 adds interacting v2 state machines for scope-threshold approval votes and authenticated fence checkpoints, with portable persistent state replacement/merge and rollback-resistant lineage. Cycle 6 adds durable ownership handoff and responsibility-limited approval rounds. Cycle 7 adds online status migration and persistent effect delivery. Cycle 8 adds isolation-integrity repair and recoverable session continuation. These machines remain observable through the same hook inspector while preserving legacy and missing-policy behavior.

Apply the development patch to a disposable repository copy, then run the public contracts:

```bash
python3 dev_cases/run_public_case.py --case dev_001 \
  --plugin-root <copy>/plugins/policy-provenance-ledger
python3 dev_cases/run_public_case.py --case dev_002 \
  --plugin-root <copy>/plugins/policy-provenance-ledger
```

The public runner reads only assets disclosed by each development case. It does not import hidden evaluator logic or reveal hidden assertions. Both commands first execute the complete original public contract, then execute the shared Cycle-9 integrity scenario in an independent workspace/state and temporary Candidate-plugin copy. The scenario reads each case's existing `assets/policy_integrity.json`; `dev_001` and `dev_002` complete 59 and 57 hook responses respectively. Any original-contract or Cycle-9 failure remains structured JSON evidence and exits nonzero.

## Evaluation

Run all hidden cases against an installed Candidate:

```bash
python3 evaluator/harness/evaluate_suite.py \
  --cases-root test_cases \
  --repository input/repository \
  --patch <submission>/solution.patch \
  --submission <submission> \
  --output-dir <evaluation-output>
```

Run one case, including its original patch preparation, with:

```bash
python3 evaluator/harness/evaluate_case.py \
  --case-dir test_cases/test_001 \
  --repository input/repository \
  --patch <submission>/solution.patch \
  --submission <submission> \
  --output-dir <case-output> \
  --result <case-output>/result.json
```

The suite applies one patch, proves that applying it twice is rejected, validates the JSON report and every changed path, verifies the existing plugin hashes, installs the plugin copy, launches the fixed entry point, and runs six workspace/state scenarios. It writes `run_summary.json` with assertion evidence, commands, durations, caps, case scores, validity, and the unweighted arithmetic mean. Patch/report/build/entry errors, evaluator-integrity failures, memory violations, or suite-wall failures invalidate the suite. After entry validity, isolated hook/inspector timeout, crash, or protocol failures are valid case-local zeroes with bounded evidence; unrelated case scores still aggregate.

Run calibration and rejection checks with:

```bash
python3 evaluator/harness/evaluate_suite.py --self-test \
  --output-dir /tmp/claude-policy-cycle9-v2-selftest
```

Hidden scenarios combine concurrent hook processes, monotonic policy reload/rollback, crash-tail recovery, compression, key/delegation rotation, nested shell/redirect permissions, weakened MCP delegation/revocation, bounded audit/lease/approval/checkpoint/handoff checks, group-constraint voting, replay and rotation, quota contention, checkpoint replacement/direct-child merge, ownership prepare/accept/finalize/activate/abort, source fencing, switch-crash recovery, settlement/expiry, and editing. They do not call Claude Code core or a provider. Cycle-7 combinations also cover Post-effect generation failure, exclusive-consumer races, nack/reclaim, declaration-expiry recovery, pending-queue authority, online-batch catch-up, partial early death, stale commits, exact commit replay, format downgrade/sibling rejection, format-aware checkpoint archives, and delivery during ownership activation. Cycle-8 combines every hidden scenario with corrupted journals, isolation, bounded/partial rebuild, independent verifier replay and forgery rejection, repair-commit recovery, exact target-session activation, source/capacity/expiry fencing, epoch continuation barriers, delegated MCP scope, checkpoint-repair/continuation refusal, ownership transfer, format/effect correlation, and legacy opt-in. Every hidden case assigns at least 70 points to the two Cycle-8 machines or their direct combinations.

All schedules are synthetic and offline. The source snapshot represents material at the public `anthropics/claude-code` upstream pin `7ef6eec9d9ba84ea6f233f26c45f1df5c5991843`; its local wrapper is documented separately in `meta/source_repo_analysis.md`.

## Cycle-9 integrity seal

The optional v2 feature deepens the plugin-update/cache boundary. A valid `integrity` policy seals five fixed plugin files and protects policy-listed workspace-relative paths. Public cases show protected-write refusal under a permissive policy, first sealing, a bounded `--integrity` view, persistent `compromised`/quarantine after harmless plugin-byte tampering, and fail-closed behavior after restart. Hidden cases combine this with repair, continuation, MCP, epoch, checkpoint, upgrade/delivery, and legacy compatibility paths. Each case adds a ten-point integrity assertion and a dedicated bypass cap, so Cycle 8 alone cannot earn full credit.

If `integrity` is absent, original behavior is unchanged. The Candidate computes seal digests at runtime from fixed plugin files; the evaluator does not provide a digest oracle. The evaluator checks only the public bounded view, hook refusal/authorization results, persistent generations, and edit constraints. All new data is local synthetic JSON and needs no network or LLM.

The public Cycle-9 sequence checks: an ordinary Read triggers the first five-file seal; normalized `Write` paths and recognizable Bash redirects are refused by the protected boundary without creating a lease, approval, or effect; `--integrity` reports generation 1, five files, two protected patterns, and `active`; and a harmless comment appended only to a temporary plugin copy is rejected by the hook, quarantined in the receipt, and still reported as the same `compromised`/quarantined digest by a fresh inspector after restoring the original bytes.

The corresponding hidden assertions are `SHELL9.INTEGRITY_SEAL_AND_PATH_FENCE`, `TXN9.INTEGRITY_SEAL_AND_PATH_FENCE`, `MCP9.INTEGRITY_SEAL_AND_PATH_FENCE`, `EPOCH9.INTEGRITY_SEAL_AND_PATH_FENCE`, `MAINT9.INTEGRITY_SEAL_AND_PATH_FENCE`, and `COMPAT9.INTEGRITY_SEAL_AND_PATH_FENCE`. The matching `CAP.*9_INTEGRITY_BYPASS` caps apply when a protected write is authorized, tampering remains authorized, or `compromised` is cleared.

Before publication, run the offline checks documented by the evaluator, including `py_compile`, JSON parsing, the Cycle-9 suite self-test, and both public cases. All checks must use fresh temporary output directories and must not contact a provider.
