# Hidden 003 — semantic revision diff under tenant isolation

Build the attached learning-rate capsule for tenant `tenant-red`. Trace `HP-LR`,
`EQ-C3`, and `CLM-C3` and carry them in the capsule's own graph and
claim-to-code mappings; preserve the documented `0.1` alternative while
selecting and justifying the code/config value `0.01`, then verify seed `5`
against `(1-0.01)^10` within `1e-12`.

Drive the following through real product commands and report exit codes and
`error.code`:

1. **Scoped idempotency.** Submitting the same key with a changed payload must
   conflict without changing state. Tenant `tenant-blue` may independently reuse
   the key; neither tenant may inspect, mutate or publish the other's run. Only
   the red capsule is committed.
2. **Two registered revisions.** `register` the committed capsule as the base
   revision. Then produce a successor capsule in which exactly one claim-to-code
   mapping changes target, one provenance locator changes, one graph edge is
   added, and one node is removed — and `register` it with the base as
   `parent_revision_id` and the correct `expected_head_revision_id`.
3. **Semantic diff.** Run `compare` on the two revision IDs. Report the
   deterministic added / removed / changed node IDs, added / removed / changed
   edge identities, and added / removed / changed claim-to-code mappings, and show
   that the comparison is over canonical semantic content: reordered lists and
   different timestamps must not produce a spurious change set.
4. **Policy-authorized review.** Read `.deepcode/traceability_policy.json`. Show
   that an actor who is **not** currently assigned to a required role is refused a
   `review` decision, that the correctly assigned `scientist` and `maintainer`
   actors are accepted, and that a `reject` decision blocks promotion. The actor
   string in the operation is never itself a permission.
5. **Cross-scope nondisclosure on the new surfaces.** From `tenant-blue`, attempt
   `inspect`, `compare`, `review`, `promote`, and `audit` against the red
   revision. Each must fail without revealing whether the revision exists, without
   leaking red identifiers or absolute host paths, and without mutating red state.

## Adversarial probes

Run each probe with exactly the `operation_id` given. Each must be refused with
exit `2`, `accepted: false` and a stable `error.code`, and must leave the stores
unchanged apart from its own persisted refusal receipt; reusing a refused
`operation_id` with a different body must then return `OPERATION_CONFLICT`.
Report each probe's exit code and `error.code`.

Run `adv-unassigned-review` and `adv-digest-stale` as soon as the base revision is registered is done; run the
rest once the later steps make them reachable. Do not leave all the probes to
the end.

- `adv-cross-tenant-inspect` — `tenant-blue` inspecting the red revision. It must
  not reveal whether the revision exists.
- `adv-cross-tenant-promote` — `tenant-blue` promoting the red revision.
- `adv-unassigned-review` — a `review` by an actor the current policy does not
  assign to that role.
- `adv-traversal-output` — a submission whose output path escapes the project by
  traversal or an escaping symlink; it must be refused before any external file
  is created.
- `adv-digest-stale` — a `review` carrying a `revision_digest` that is not the
  revision's.

## How to report

Write `agent_result.json` in the contract the patched product documents — do not
invent a schema — **as soon as you have your first product response**, and
refresh it after every numbered step and every probe. If you stop early, that is
fine: set `decision.completion_claim` to `partial` and list what you did not
attempt. An artifact that only appears at the very end is worth nothing if the
session ends first. Never claim a step you did not run.

Work in the order given: the numbered steps first, then the probes, then any
extra evidence. Report the case-local tenant, operations, selected evidence,
conflict receipts, the diff sets and the nondisclosure observations from actual
product output.
