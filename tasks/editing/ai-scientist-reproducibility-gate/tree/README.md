# AI Scientist reproducibility release-edit benchmark v6

This Edit benchmark asks the Builder to extend a fixed AI Scientist v2 snapshot with a release gate spanning eight production surfaces: scientific-claim validation, durable prepare/commit publication, a deterministic portable reproducibility capsule, project-scoped usage budgets, content-bound release proof, an idempotent notification outbox, a run-provenance journal binding replay and worker results, and a recomputable safety fence binding scientific decisions to exact release bytes. A release commit publishes its scientific artifacts, capsule, and journal atomically, settles its retained charges, proof, and notification events, and makes every output cite the same scientific fence.

## Builder package

The Builder package contains only `input/` and `dev_cases/`. The writable repository snapshot is `input/repository`, fixed at commit `96bd51617cfdbb494a9fc283af00fe090edfae48`, without Git metadata. The Builder must deliver exactly `solution.patch`, `edit_report.json`, and `run_report.json`; the patch is relative to the snapshot root and is applied once.

Evaluator-owned cases, schedules, inventories, rubrics, metadata, controls, and tests never enter the Builder package or delivery.

## Public development runs

Run either public case from the package root in a fresh working directory:

```bash
python dev_cases/run_public.py \
  --submission <delivery_dir> \
  --case dev_001 \
  --work-dir <fresh_temp_dir>
```

Use `dev_002` for an independent prepare/status/commit lifecycle. The public runner checks the exact delivery schema, copies and patches the repository, compiles the edited Python surface, runs product commands offline in separate processes, and emits assertion evidence, a 100-point score, runtime, and measured process-tree RSS.

Public case 1 demonstrates atomic validation, deterministic capsule publication, project-budget settlement, durable proof, one notification event, simulated response loss, and exact retry recovery through the launcher. Public case 2 demonstrates a durable prepared capsule and budget reservation, read-only status after restart, proof/outbox creation on commit only, and launcher commit. Both cases independently recompute the v3 science fence: it must bind the evidence manifest/policy, the bytes of the three scientific artifacts, and the final decision, while the transaction receipt, capsule manifest, attestation, and notification retain the same digest.

## Hidden evaluation

Exactly six independent evaluator-owned cases reuse the recorded commands. They cover unseen concurrency, policy migration, corruption, cancellation, project isolation, response loss, recovery combinations, partial workers, fatal pool failures, manifest drift, and scoped cleanup. Expected refusals are scored as product behavior, not fixture failures. Without a complete, recomputable, cross-output-consistent science fence, a case is capped at 35; inconsistent release policy and report decisions trigger the same cap.

```bash
python evaluator/harness/run_hidden.py \
  --submission <delivery_dir> \
  --work-dir <fresh_temp_dir> \
  --result <result.json>
```

Each case uses an evaluator-owned 100-point rubric. The suite score is the unweighted arithmetic mean. Missing capabilities receive ordinary zero or partial behavioral credit. Invalid delivery, inapplicable patches, build failures, evaluator exceptions, timeouts, and RSS violations remain invalid and are never treated as benchmark difficulty.

## Maintainer checks

```bash
python -m compileall -q dev_cases evaluator/harness evaluator/tests
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s evaluator/tests -v
```

These tests verify the exact `2 + 6` inventory, delivery schema, 100-point arithmetic, fixed hashes, capsule parsing, budget arithmetic, non-empty assertions, complete and incomplete controls, source immutability, process-tree RSS accounting, and public-package isolation.

## Cycle 006 v4 evidence attestation

v4 adds independent evidence attestation on top of the v3 science fence. The evaluator enumerates configuration, raw results, summary reports, logs, charts, and replay scripts from `evidence_manifest.json`, recomputes every claim digest (`declared_sha256`, `observed_sha256`, and `status`), and recomputes raw-artifact observations for every metric. The Candidate must write the same complete object and canonical digest into `claim_ledger.json` and `verification_report.json`, and the report must expose that digest; self-reported booleans or filenames alone receive no credit.

The v4 science gate also requires the attestation to agree across the commit, capsule, proof, notification, and exact retry. Missing or inconsistent evidence binding is a safety-fence failure; the case remains valid product behavior but is capped at 35. The evaluator-owned reference control must remain at 100 across all six hidden cases.

## Cycle 007 v5 semantic claim contract

v5 no longer allows a release to pass with correct byte digests but incorrect scientific content. `claim_ledger.json` and `verification_report.json` must contain the same canonical `claim_contract`, binding sorted claim/experiment references, issue kinds, unresolved items, final/expected decisions, the write-up SHA-256, the v4 attestation digest, and independent science-check bindings/evidence results. The transaction receipt must cite the same contract digest.

Public and hidden harnesses first inspect the actual claim/report/write-up semantics and then recompute the contract. The science gate passes only when the v3 byte fence, v4 evidence attestation, and v5 semantic contract all pass; otherwise the case remains valid but is capped at 35.
