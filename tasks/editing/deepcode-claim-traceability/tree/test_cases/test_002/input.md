# Hidden 002 — lease takeover and checkpoint-resumable execution

Produce a verified partial capsule for `ALG-S2`, `GAP-N2`, and `CLM-S2` in the
attached sampling project, and carry those identifiers in the capsule's own graph
and claim-to-code mappings. Re-run 200 draws for seeds `7`, `19`, and `31`;
probabilities must be finite, nonnegative, sum to one within `1e-12`, and each
seed's result must be stable. Record the unnormalized-softmax implementation as
an explicit gap rather than silently correcting the scientific claim.

The primary failure boundary is restart fencing across both durable surfaces.
Drive it through real product commands and report exit codes and `error.code`:

1. **Durable lease takeover.** Generation 1 leaves an incomplete stage and
   expires. A new process claims generation 2, stale generation-1 actions are
   rejected, the untrusted stage is never executed, and the run finishes under
   the valid owner generation with an idempotent commit retry.
2. **Registration and plan binding.** `register` the verified capsule, then
   `start` an execution plan bound to that exact `revision_digest` and the current
   review generation. Report the returned `plan_id`, generation `1`, the step
   count, and the next step. Repeat `start` with a changed payload and show it
   conflicts.
3. **One command per checkpoint.** `advance` the plan one command at a time.
   Report every checkpoint's ordered sequence number, command identity, exit code,
   bounded output digest, and claimed artifact digest. Replay one accepted
   `advance` exactly and show the checkpoint is not re-executed and no duplicate
   sequence number appears.
4. **Pause and resume.** `pause` with `QUOTA_EXHAUSTED`, retry the pause exactly,
   then `resume`: the generation increments once, the claim token rotates, and the
   successful checkpoint prefix survives. Show that the **old** generation and the
   **old** claim token can no longer `advance`, write a checkpoint, or produce an
   attestation.
5. **Attestation.** Complete the remaining steps and show the recorded execution
   attestation is bound to the same `revision_digest` and review generation as the
   plan.

## Adversarial probes

Run each probe with exactly the `operation_id` given. Each must be refused with
exit `2`, `accepted: false` and a stable `error.code`, and must leave the stores
unchanged apart from its own persisted refusal receipt; reusing a refused
`operation_id` with a different body must then return `OPERATION_CONFLICT`.
Report each probe's exit code and `error.code`.

Run `adv-digest-stale` and `adv-opid-reuse` as soon as the revision is registered is done; run the
rest once the later steps make them reachable. Do not leave all the probes to
the end.

- `adv-digest-stale` — a `start` bound to a `revision_digest` that is not the
  registered revision's.
- `adv-stale-token` — an `advance` carrying the claim token the plan held before
  the resume rotated it.
- `adv-stale-generation` — an `advance` carrying the pre-resume `generation`.
- `adv-corrupt-replay` — a `commit` of the deliberately corrupted replay staging.
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
extra evidence. Report observed generations, checksums, replay outcome, terminal
state, checkpoints and receipts from actual product output.
