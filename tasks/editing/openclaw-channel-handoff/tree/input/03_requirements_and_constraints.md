# Requirements and constraints

## Functional requirements

1. Bind the exact provider/account/peer channel identity to a principal.
   Task IDs, display names, copied tokens, stale grants, and attachment
   addresses never authorize access.
2. Support root grants and weakening child grants. A child permission set
   is a subset of its parent's, `summary` cannot delegate `full`, lineage
   depth is bounded, and a parent revoke/rotation transactionally
   invalidates the whole old subtree.
3. Persist events, operations, projections, and response replay in the
   shared SQLite. A duplicate scoped key replays one committed response
   across client processes and restarts; mismatched key/event pairs and
   stale CAS writes fail without mutation.
4. Restrict mutation per task to one live gateway owner. A contender
   sharing the state database cannot mutate during a valid lease. Expired-
   owner takeover advances the durable owner epoch, and every stale
   owner/process write fails.
5. Maintain a durable logical clock and integer epochs. Rolling back
   `Date.now()`, restarting, or lowering client event times must not
   decrease epochs, extend expired leases, restore permissions, or reorder
   committed events.
6. Admit effects through a transactional outbox and send only to the
   evaluator-configured loopback sink. The task event and the pending row
   commit together. Concurrent dispatch, crashes after sink acceptance,
   restarts, and takeover converge to one sink acceptance and one completed
   effect, using the stable effect ID as the downstream idempotency key.
7. Compute attachment SHA-256 and size from the supplied bytes, store the
   bytes at the content address, and authorize every task reference
   independently. Equal content may be stored deduplicated but must not
   share permission. Projections expose only metadata; `attachment.read` is
   the only principal-facing path.
8. Enforce source-channel-plus-principal attachment visibility and both
   independent conditions for full result disclosure. Revoked/rotated
   lineage cannot read attachments or results.
9. Freeze user-result and internal-control deliveries in a durable receipt
   queue. A pull creates a replayable, expiring claim and never deletes
   content; only an explicit acknowledgement marks the pull-consumer path
   complete. Concurrent pullers obtain at most one live claim, an expired
   claim redelivers the same bytes, and the current
   grant/capability/disclosure permission fences both pull and
   acknowledgement.
10. Distinguish task completion from channel delivery. Pending, claimed,
    and acknowledged metadata survive owner takeover, restart, compaction,
    and repair, while content, source routes, and claim tokens stay out of
    projections. An internal-control payload never crosses its exact
    source channel on the basis of text or a broad result permission.
11. Deliver human-visible results through the configured channel connector
    boundary using an immutable provider/account/peer/destination-type
    route and a stable dispatch identity. Connector acceptance is
    unverified; only a reconciled non-empty platform message identity
    marks a verified delivery. Lost responses, duplicate requests,
    restarts, and owner takeover must not post twice. Internal control,
    stale lineage, foreign routes, empty identities, and out-of-order sends
    fail closed without leaking content or credentials.
12. Support task schema 1 and the current schema 2. Schema-1 creation is
    limited to legacy events. `handoff.migrate` upgrades one task
    transactionally and idempotently; an interruption leaves exactly the
    old or the new valid schema. Future schemas and unknown RPC fields fail
    closed.
13. Produce generation-linked compaction proofs and bounded inspection.
    Verify the current projection/event/proof relationship through the
    production RPC. Repair only derived projection/proof state from
    authorized durable facts; never drop events, reset monotonic counters,
    restore grants, or redeliver effects.
14. Preserve deterministic final state, pending continuations, completed
    effects/subagents, attachment references, migration, ownership, and
    proof state across SIGKILL and independent gateway restarts.
15. Add focused tests for the public methods, two-gateway
    contention/takeover, weakening/revocation races, outbox crash
    recovery, content-addressed disclosure, delivery claim/ack crash
    recovery, verified connector dispatch and receipt reconciliation,
    migration, repair, compaction, and compatibility of the existing
    gateway protocol.
16. Schedule every media-bearing delivery from an authorized content-
    addressed attachment into a server-computed immutable manifest and
    ordered chunk list. Provider init, every chunk, finalize, and status
    reconciliation use stable identities and exact bodies. Lost responses,
    restarts, and owner takeover resume only from provider-observed chunk
    boundaries and cannot repeat a logical media upload under a new
    identity.
17. Separate provider upload completion from channel acceptance and final
    platform verification. Unfinished or unauthorized media items are
    rejected before channel I/O; a media-bearing delivery cannot silently
    degrade to plain text. Partial, duplicate, out-of-order, empty, or
    mismatched media receipts cannot verify the logical message. Compaction
    and repair protect cursors and completed upload facts without
    repeating provider or channel effects or exposing sessions,
    principals, credentials, or receipt identities.
18. Prepare, for every verified inbound interaction, one user-result
    delivery and its immutable source route. Callback tokens are verified
    only through the configured interaction adapter using a stable ingest
    identity and the exact body. A duplicate provider event replays one
    durable interaction; reusing it with a changed action, body,
    correlation, or route fails before adapter or channel I/O.
19. Recover adapter-acceptance ambiguity through status reconciliation on
    restart or owner takeover. Stale owners, retired lineage, foreign
    routes, forged callback tokens, mismatched provider identities, or an
    unknown status must not create an interaction. Concurrent gateways
    converge to one adapter acceptance, one verified interaction, and at
    most one durable claim.
20. Complete only the current live interaction claim. Atomic completion
    records the processed provider event and freezes one correlated
    user-result delivery on the inherited direct/group/thread route.
    Response replay, claim expiry, rotation, compaction, repair, migration,
    and rollback never duplicate agent turns or replies. Projections expose
    bounded summary state metadata only, never callback/reply bodies,
    callback tokens, claims, adapter credentials, or provider identities.

21. Answer the evaluator invariant probes. After the initial state of each
    case is materialized, the evaluator issues a fixed set of Gateway calls
    against the task with its own client, recorded as
    `origin: "environment_invariant_probe"`. They are issued by the
    evaluator and consume no lower-agent turns; they measure the product
    rather than drive it. The product must answer them by the published
    semantics:
    - `handoff.status` returns a bounded current projection;
    - a never-issued grant, an unknown RPC field, a stale
      `expected_revision`, and a `handoff.delivery.enqueue` with
      `intent:"internal_control"` paired with `visibility:"principal"` must
      all be structurally refused and commit nothing;
    - on a schema-2 task: `handoff.attachment.put` returns an
      `attachment_id` equal to the server-computed content address
      (`sha256:<64 lowercase hex>`) plus the correct `sha256` and `size`;
      an exactly identical duplicate returns `replayed:true` and the same
      `attachment_id`; the same `idempotency_key` with different bytes
      conflicts and commits nothing; `handoff.attachment.read` returns the
      same bytes; a `handoff.append` with `occurred_at_ms` set back ten
      days must not decrease any stored counter (accepting it must advance
      `revision` without changing `owner_epoch`/`capability_epoch`;
      rejecting it likewise satisfies monotonicity);
      `handoff.integrity.verify` returns `valid:true` and a 64-hex
      `projection_digest`; `handoff.compact` advances to
      `generation >= 1` and returns a `compaction_proof` with a 64-hex
      `projection_digest` and `generation_digest`; the immediately
      following `handoff.status` must not reset
      `revision`/`owner_epoch`/`capability_epoch` and must not lose that
      attachment reference;
    - on a schema-1 task: `handoff.compact` must be refused before
      `handoff.migrate`;
    - when the case has a foreign channel lineage, that lineage's
      `handoff.status` on this task must be refused.
22. Make the RPC contract discoverable. `node openclaw.mjs gateway call --help`
    must list every `handoff.*` method and its exact required parameter
    names. Any `INVALID_REQUEST` must list all missing and invalid fields
    in one response (`error.details.missing` and `error.details.invalid`
    arrays) rather than reporting one field at a time. `error.code` is
    stable and enumerable. Round trips to discover the product's shape
    should not consume the case budget.
23. Support an incrementally emittable agent-loop result contract.
    Document and support a way to write `agent_result.json` right after
    the first product response and refresh it after every subsequent step,
    containing `decision_state` (`complete` or `partial`) and an explicit
    list of unattempted/unverified items; stopping midway must leave an
    honest partial result rather than no result. `run_report.json` carries
    the same case identifier.
24. Refusal is terminal. A refused operation commits no mutation, appends
    no ledger event, triggers no connector/provider/adapter I/O, and
    returns a stable `error.code`. Reusing a refused operation under the
    same scoped operation key but with a changed body must fail as a
    conflict.

## Constraints

- Work only in the permitted production directories and the designated
  co-located tests named in `02_interface_and_delivery.md`.
- Use the shared `$OPENCLAW_STATE_DIR/state/openclaw.sqlite`; add no
  JSON/JSONL permission, outbox, attachment, lease, or migration sidecars.
- Use local deterministic data. The only valid destination is the loopback
  URL provided in `OPENCLAW_HANDOFF_EFFECT_SINK_URL`; reject missing,
  non-loopback, redirecting, or credential-bearing destinations.
- The only human-channel boundary is the evaluator-owned loopback base URL
  and bearer token provided in `OPENCLAW_HANDOFF_CHANNEL_CONNECTOR_URL` and
  `OPENCLAW_HANDOFF_CHANNEL_CONNECTOR_TOKEN`. Reject missing, non-loopback,
  redirecting, credential-bearing, or path-confused URLs. Never contact
  real channels or treat connector acceptance as verified delivery.
- The only provider media boundary is the evaluator-owned loopback base URL
  and bearer token provided in `OPENCLAW_HANDOFF_MEDIA_CONNECTOR_URL` and
  `OPENCLAW_HANDOFF_MEDIA_CONNECTOR_TOKEN`. Reject missing, non-loopback,
  redirecting, credential-bearing, or path-confused URLs. Never send
  attachment bytes to the human-channel connector before a verified
  provider upload, and never substitute one connector credential for
  another.
- The only inbound interaction verification boundary is the evaluator-owned
  loopback base URL and bearer token provided in
  `OPENCLAW_HANDOFF_INTERACTION_CONNECTOR_URL` and
  `OPENCLAW_HANDOFF_INTERACTION_CONNECTOR_TOKEN`. Reject missing,
  non-loopback, redirecting, credential-bearing, or path-confused URLs.
  Keep that service credential separate from callback tokens and from the
  effect, media, and human-channel credentials.
- Do not hard-code case entities, task/event/effect IDs, payloads, ports,
  process IDs, clocks, evaluator paths, or secret values.
- Use the exact RPC and submission schemas in
  `02_interface_and_delivery.md`. Do not add alternative binaries,
  evaluator adapters, debug mutation RPCs, or fault-injection hooks.
- Work non-interactively. Public/formal commands allow an actual maximum of
  24 GiB process-tree PSS, a 6,900-second suite budget, a 930-second
  envelope per case (≤300 s evaluator preparation + 600 s of the agent's
  own wall clock + 30 s cleanup and forensics), a designated 120-second
  startup budget per gateway, and no virtual address limit or `--jitless`.
