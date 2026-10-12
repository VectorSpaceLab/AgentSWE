# Evaluator instructions

Use `evaluator/harness/evaluate_suite.py` as the authoritative evaluator. It
receives one pristine pinned repository, the frozen candidate submission, six
hidden case input/assets, the case-unique Python prefix, and an empty output
path.

1. Verify the exact hidden inventory and public-runner isolation. Validate
   the UTF-8 patch/reports/provider counts/paths/symlinks, apply the patch
   once, and prove that it cannot be applied a second time.
2. Prepare/install once, compile/import the pinned modules, and record the
   candidate's focused tests and the pinned Aider compatibility tests without
   scoring their claims as production behavior.
3. Run all six new local scenarios only through the public schema-v3 JSON
   adapter. Fixture argv, Git-common-dir admission, quarantine, hooks,
   filters, nested submodules, refs, object pruning, linked worktrees, and
   foreign writers are evaluator-owned local resources.
4. Establish results independently from canonical repository identity,
   admission bytes, exact ref OIDs before/after each participant transaction,
   ordinary versus quarantined object visibility, durable SHA-256/Git-OID
   closures, commit ancestry/trees/gitlinks, index/filesystem snapshots,
   process-group death, locked command/hook logs, worktree registrations, and
   Linux `/proc` PSS. Responses and ledger/object bytes are untrusted claims.
5. Continue after every candidate behavior failure. Convert a local
   launch/timeout/crash/malformed-response/exception into that case's full
   zero-assertion checklist. Apply caps only for genuine local integrity or
   authority violations described in the headings.
6. Refuse to aggregate unless exactly six results exist and each is
   `valid:true` with exact assertion IDs, weights, earned/raw totals, caps,
   and final scores. Missing, malformed, duplicate, evaluator-error, or
   integrity-invalid results invalidate the suite instead of silently scoring
   zero.
7. Cite bounded concrete evidence for every finding. Redact the evaluator
   root, fixture/user markers, object payloads, admission/lease tokens, and
   candidate output beyond the bounded tail. Ignore implementation choices
   and reference-source similarity.

Canonical invocation:

```bash
${BENCHMARK_ROOT}/envs/aider-worktree-transaction-ledger-edit-v1/bin/python \
  evaluator/harness/evaluate_suite.py \
  --repository input/repository \
  --patch /submission/solution.patch \
  --submission /submission \
  --cases-root test_cases \
  --python-env ${BENCHMARK_ROOT}/envs/aider-worktree-transaction-ledger-edit-v1 \
  --output-dir /evaluation/aider-repository-set-transaction-v3
```

The final `run_summary.json` must report suite validity/build/install/test
evidence, public isolation/apply-once findings, all six explicitly valid case
results, commands/durations/peak PSS, local caps/errors, and the arithmetic
mean.
