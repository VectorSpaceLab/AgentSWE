# Hidden 005 — cancel fencing and atomic head compare-and-swap

Build the attached ablation capsule for `ALG-E5`, `ABL-GATE`, and `CLM-A5`, and
carry those identifiers in the capsule's own graph and claim-to-code mappings.
Run seed `23` in full and no-gate configurations; verify outputs `[3,5,7]` and
`[2,4,6]`, means `5` and `4`, and improvement `1`. Bind the claim to the code,
configuration, tests, commands, and distinct artifacts.

Drive the following through real product commands and report exit codes and
`error.code`:

1. **Stale-owner exclusion during atomic commit.** After generation 2 becomes
   current, generation 1 must not commit even a checksum-valid stale capsule.
   Exact operation and idempotency retries preserve one run, one publication
   digest and one generation.
2. **Plan cancellation fence.** `register` the published capsule, `start` a plan
   bound to its exact `revision_digest`, and `advance` one checkpoint. Then
   `cancel` the plan with `expected_generation`, and show that no artifact or
   attestation is produced after the cancel.
3. **Replacement generation.** Start the replacement generation for the same
   revision, keep the verified checkpoint prefix, finish the remaining commands
   exactly once, and show the recorded attestation belongs to the live generation
   only.
4. **Atomic head swap.** Obtain the required current reviews, then `promote` with
   the matching digest, review generation, `plan_id` and `plan_generation`. Show
   that (a) a promotion carrying the cancelled plan's generation fails closed,
   (b) a concurrent or replayed promotion with a stale
   `expected_head_revision_id` cannot overwrite the newer head, and (c) after the
   successful promotion the head, the promotion record, and the plan attestation
   agree on one revision digest.
5. **Audit of the fenced sequence.** Export the scoped `audit` record and show the
   ordered events for register, review, cancel, promote, the chain head hash, and
   `chain_valid`, with no event recorded for any refused stale operation.

## Adversarial probes

Run each probe with exactly the `operation_id` given. Each must be refused with
exit `2`, `accepted: false` and a stable `error.code`, and must leave the stores
unchanged apart from its own persisted refusal receipt; reusing a refused
`operation_id` with a different body must then return `OPERATION_CONFLICT`.
Report each probe's exit code and `error.code`.

Run `adv-stale-commit` and `adv-opid-reuse` as soon as step 1 is done; run the
rest once the later steps make them reachable. Do not leave all the probes to
the end.

- `adv-stale-commit` — generation 1 committing its checksum-valid stale capsule
  after generation 2 became current.
- `adv-cancelled-advance` — an `advance` with the cancelled generation's claim
  token; no checkpoint, artifact or attestation may result.
- `adv-cancelled-promote` — a `promote` carrying the cancelled plan's generation.
- `adv-stale-head-promote` — a replayed `promote` whose
  `expected_head_revision_id` is the head from before the successful promotion.
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
extra evidence. Report generation fencing, observed reader states,
publication/validation receipts, checkpoints and ablation evidence from actual
product output.
