# Hidden 004 — reconciling interrupted work across both stores

Build the attached train-only normalization capsule for `DATA-SPLIT`,
`PREP-ORDER`, and `CLM-D4`, and carry those identifiers in the capsule's own
graph and claim-to-code mappings. Re-run seed `13`, use rows 0..5 for training
and 6..7 for test, fit mean and population standard deviation on training data
only, and verify normalized training mean zero and variance one. Record the
ordered data-manifest steps and source checksums.

Drive the following through real product commands and report exit codes and
`error.code`:

1. **Partial-write reconciliation.** Generation 1 leaves truncated JSON and
   unchecked output in staging. On expiry, reconcile it without running the
   experiment from untrusted bytes; a repeated reconcile is a deterministic
   no-op. A current owner then rebuilds from source and publishes one complete
   terminal capsule.
2. **Registration and checkpointed rebuild.** `register` the published capsule,
   `start` a plan bound to that exact `revision_digest`, and `advance` it one
   command at a time. Report every checkpoint's sequence number, command identity,
   exit code and bounded output digest. Make one manifest command fail and show
   the plan pauses with the successful prefix intact and is **not** reported as
   complete.
3. **Execution reconcile without execution.** Run the execution store's
   `reconcile`. It must not execute any manifest command. Show that a second
   `reconcile` with unchanged state is a no-op with the same deterministic counts
   and affected IDs.
4. **Corrupted immutable snapshot.** Corrupt the registered revision snapshot on
   disk, then run the revision store's `reconcile`. It must flag or quarantine the
   corrupted revision, invalidate its review and execution eligibility, roll the
   head back to the latest intact promoted ancestor if one exists, and report
   terminal corruption — never re-run the experiment and never convert the run to
   a successful state. Show that the plan bound to that revision is now
   invalidated and cannot `advance`.
5. **Audit continuity.** Export the scoped `audit` record afterwards and show that
   the reconciliation and invalidation appended ordered events, that the chain head
   advanced, and that `chain_valid` is still true.

## Adversarial probes

Run each probe with exactly the `operation_id` given. Each must be refused with
exit `2`, `accepted: false` and a stable `error.code`, and must leave the stores
unchanged apart from its own persisted refusal receipt; reusing a refused
`operation_id` with a different body must then return `OPERATION_CONFLICT`.
Report each probe's exit code and `error.code`.

Run `adv-untrusted-stage` and `adv-corrupt-checksum` as soon as step 1 is done; run the
rest once the later steps make them reachable. Do not leave all the probes to
the end.

- `adv-untrusted-stage` — an attempt to advance or commit using the interrupted
  staging bytes; the product must never execute the staged `replay.py`.
- `adv-corrupt-checksum` — a commit of the capsule whose checksum you corrupted;
  it must be refused before any sentinel side effect.
- `adv-advance-invalidated` — an `advance` of the plan bound to the revision you
  corrupted; it must be refused after reconciliation invalidated it.
- `adv-reconcile-execute` — a `reconcile` issued while a manifest command would
  be runnable; reconciliation must return without executing any command.
- `adv-opid-reuse` — reuse the `operation_id` of one of the refused probes above
  with a different body; it must return `OPERATION_CONFLICT` and mutate nothing.

## How to report

Write `agent_result.json` in the contract the patched product documents — do not
invent a schema — **as soon as you have your first product response**, and
refresh it after every numbered step and every probe. If you stop early, that is
fine: set `decision.completion_claim` to `partial` and list what you did not
attempt. An artifact that only appears at the very end is worth nothing if the
session ends first. Never claim a step you did not run.

Work in the order given: the numbered steps first, then the probes, then any
extra evidence. Report reconciliation actions, checksum evidence, generations,
checkpoints, terminal receipts and honest corruption status from actual product
output.
