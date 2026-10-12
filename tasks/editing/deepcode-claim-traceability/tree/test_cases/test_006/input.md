# Hidden 006 — blocked claim, policy drift, and security quarantine

Prepare an inspectable capsule for `EQ-K6`, `CITE-K6`, `GAP-K6`, and `CLM-K6`,
carrying those identifiers in the capsule's own graph and claim-to-code
mappings. The value `kappa` exists only in unavailable citation `SYN-CITE-17`.
Do not use the network or invent the value. Identify the missing definition, the
affected code/tests/claim, and the evidence that would unblock reproduction.

Drive the following through real product commands and report exit codes and
`error.code`:

1. **Fail-closed blocked publication.** Reject an incomplete submission without
   publishing it, then let a structurally valid blocked capsule reach a durable
   `blocked` terminal state with an idempotent exact retry. If published evidence
   is corrupted, reconciliation reports terminal corruption and never converts the
   run to complete/partial. The ordinary untracked project stays byte-identical.
2. **Blocked revisions are not promotable.** `register` the blocked capsule as a
   revision, obtain the reviews the current policy requires, and show that
   `promote` still fails closed because the revision is blocked and carries no
   complete execution attestation.
3. **Policy drift.** Change `.deepcode/traceability_policy.json` so that
   `policy_version` increases and one required role's membership changes. Show
   that the previously recorded approvals are now stale for promotion, that
   `inspect` reports the current policy evaluation against the unchanged immutable
   snapshot, and that the snapshot itself was not rewritten.
4. **Security quarantine.** As a `security` actor, `quarantine` the revision with
   a bounded reason. Show that a non-security actor is refused, that an exact
   retry is stable, and that while the quarantine is active a new `start` and any
   `promote` both fail closed.
5. **Fenced restore.** `restore` the revision with `expected_quarantine_generation`.
   Show that a stale quarantine generation and a stale policy version are both
   refused, that the restore does not erase the quarantine evidence, and that the
   reason and actor were stored as bounded identifiers and never executed.
6. **Audit fail-closed.** Export the scoped `audit` record as an authorized
   auditor and report the ordered events, head hash and `chain_valid`. Then tamper
   with the ledger and show the export fails closed rather than returning a
   plausible record.

## Adversarial probes

Run each probe with exactly the `operation_id` given. Each must be refused with
exit `2`, `accepted: false` and a stable `error.code`, and must leave the stores
unchanged apart from its own persisted refusal receipt; reusing a refused
`operation_id` with a different body must then return `OPERATION_CONFLICT`.
Report each probe's exit code and `error.code`.

Run `adv-nonsecurity-quarantine` and `adv-audit-tamper` as soon as the revision is registered is done; run the
rest once the later steps make them reachable. Do not leave all the probes to
the end.

- `adv-blocked-promote` — a `promote` of the blocked revision after the required
  reviews are in place; it must still fail closed.
- `adv-stale-policy-restore` — a `restore` carrying the pre-drift `policy_version`.
- `adv-nonsecurity-quarantine` — a `quarantine` by an actor the policy does not
  assign to `security`.
- `adv-quarantined-start` — an execution `start` while the quarantine is active.
- `adv-audit-tamper` — edit one stored audit event's payload on disk, then run
  `audit` as the authorized auditor: it must fail closed rather than return a
  plausible record. Restore the ledger afterwards and say so.

## How to report

Write `agent_result.json` in the contract the patched product documents — do not
invent a schema — **as soon as you have your first product response**, and
refresh it after every numbered step and every probe. If you stop early, that is
fine: set `decision.completion_claim` to `partial` and list what you did not
attempt. An artifact that only appears at the very end is worth nothing if the
session ends first. Never claim a step you did not run.

Work in the order given: the numbered steps first, then the probes, then any
extra evidence. Report the missing-citation boundary, blocked receipt, replay
evidence, corruption result, quarantine/restore receipts and the safe next step
from actual product output.
