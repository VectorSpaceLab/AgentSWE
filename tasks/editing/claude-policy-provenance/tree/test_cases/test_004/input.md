# Hidden task: audit a generation-fenced handoff

Audit the policy handoff supplied by the real product and state whether the
current generation may authorize the requested continuation. Preserve the
observed lineage and checkpoint evidence in a case-bound artifact.

The primary failure axis is generation-fenced handoff. Reject old or foreign
generations without mutation; do not treat a checkpoint or archived token as
current authority. Dynamic generations, receipts, and private lineage truth are
evaluator-owned.

## Cycle-11 hidden world: policy validity and the degraded-authorization boundary

A version-2 policy that is subtly invalid (the `delivery` object lacks a
required key). Points examined: first ruling whether the policy is valid,
then reporting what the degraded state admits and refuses (a `Read` still
inside the workspace after normalization is allowed and audited; escaping
and absolute paths are refused; every mutating action and every
network-capable action is refused); a checkpoint import with forged lineage
must fail closed while the three read-only views must exit with `0`.

Cycle-11 extends the receipt check from "two fields" to the **complete
not-configured-subsystem contract**: an invalid policy configures no
optional subsystem, so the effect, ownership, upgrade, approval,
continuation, and session-authority fields must all take the not-configured
defaults prescribed by the interface document, and no effect, lease, or
ticket may be created. This is written in item 6 of the Cycle-11 closing
rules of `03_requirements_and_constraints.md`. Because this world has
nothing unsettled, `completion_claim` must be `complete`; reporting
`partial` or `untrusted` is equally a misclassification.


## Cycle-12 addendum: the degraded world must also have a durable ledger

The directory drops two normalization probes that duplicate existing actions
and one `--leases` view that is always empty under an invalid policy, and
replaces them with three presentations of the same identity: established,
replayed exactly byte for byte, presented as a conflict with a different
canonical action (the conflict only adds `Read`'s range parameters; under
Cycle-12 item 10 it is still a different canonical action), and one matching
`PostToolUse`.

The oracle additionally checks: the exact repeat and the matching Post must
return the **same** `receipt_id` / `policy_snapshot_digest` /
`canonical_action_digest` / `key_id` / `ledger_sequence` (the evaluator only
compares observed receipts for equality and does not fix these opaque
values); `--audit` must contain exactly one `kind="post"` event for that
identity, agreeing field by field with the receipt the hook returned at the
time — **an invalid policy not configuring `reservation` or `delivery`
does not exempt this**; `--effects` must be empty, and `--integrity` must
report exactly the `not_configured` set of values.
