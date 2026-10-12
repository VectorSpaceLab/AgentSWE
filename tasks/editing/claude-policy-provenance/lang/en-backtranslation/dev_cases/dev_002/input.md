# Public case: rewrite, audit-only, and policy reload

Create the Policy Provenance Ledger plugin described in the global contract.

The public fixture in `assets/` rewrites
`curl https://example.test/status` to `printf offline-status` and marks
`pwd` audit-only. Invoke PreToolUse for the rewrite event. Atomically
replace the policy before its PostToolUse. Verify the complete rewrite
`updatedInput`, a stable receipt, one `pre` and one `post` journal event,
and an unchanged revision, policy snapshot digest, key ID, ledger sequence,
and canonical digest across the hooks. The `pwd` event must return
permission `allow` and decision `audit_only` under revision 2.

The next fresh rewrite event must be denied under revision
`public-reload-v2`. Attempt to reinstall epoch 10 after epoch 11 is pinned;
the new action must bind to epoch 11 and fail closed instead of rolling the
policy back.

Use `--mint-delegation` to create a root MCP delegation for epoch 11 and a
child limited to `mcp__files__inspect`, `src/public/`, and `example.test`.
The child must authorize the supplied MCP event, while a requested
child that broadens its paths cannot be created. Run inspector compaction
and key rotation. The old child stays verifiable across the rotation until
`--revoke-delegation` revokes it; after revocation the same delegation is
refused for a new event. Evidence must stay parseable and must not expose
delegation tokens or the fixture bearer token supplied in the PostToolUse
result. A fresh contained Read after rotation must use a different key ID
while earlier events remain inspectable.

Run `python3 dev_cases/run_public_case.py --case dev_002 --plugin-root
plugins/policy-provenance-ledger`. Deliver the patch and reports.

The reloaded epoch-11 policy declares a global reservation limit of 2, a
per-session limit of 1, and 2-second leases. Hold the rewrite Pre unsettled
while replacing the policy and completing the rewrite Post; the receipt must
stay pinned and the lease must settle under epoch 10. Race two fresh
epoch-11 reads in one bounded write queue, then overlap independent
`--leases` and `--reconcile` views in a small maintenance queue. Settle
one, then fill its released slot after a short delay while leaving the other
reservation unposted. Once only the older reservation has expired, the next
read must reclaim that slot, prove that the stale Post is refused, and show
no raw tool result or token in the reservation evidence.

Also create a two-principal approval ticket for an epoch-11 Write. Cast one
vote, rotate that principal, and show that its old vote and token are no
longer valid. Re-enroll the new generation, reach the threshold with the
second principal, consume and settle the ticket, then revoke a separate
ready ticket before consumption. Approval state must survive ledger key
rotation and stay bound to epoch 11.

The duty-separation groups set two principal rules: `public-a` and
`public-c` are both release and cannot satisfy the security group.
Supersede a ready-but-unconsumed round concurrently, prove exactly one
successor round with no votes carried over, then complete it with
`public-b`. Rotate and re-enroll without changing the policy-derived
groups.

Export the epoch-11 state as a base checkpoint and replace-import it into
three fresh evidence archives. Every managed archive must remain
hook-fenced. Export two direct checkpoint children and merge them into an
unchanged archive base so that one lineage wins. Exact replays are
idempotent, and the stale sibling must not move the lineage. The checkpoint,
approval, lease, and audit views stay bounded and redacted; live authority
transfers only through the handoff below.

Prepare a separate handoff to `public-branch-a`, accept it, then abort
before finalization and prove that the source recovered while the standby
never authorized. For a second handoff, kill the evaluator-owned accept
process after only part of the request is processed; the target must expose
no state, and a complete accept retry must recover. Race exact
finalize/activate replays, keep the source and standby fenced at their
respective boundaries, activate one target generation, and continue the
linked epoch policy. Handoff secrets and sealed state never appear in
diagnostics or views.

Create two epoch-11 effects around a key rotation. Claim one authorized
generation, post its result, and show that the stale claim cannot
acknowledge the new completed generation. Nack and reclaim the second
effect; exact nack/ack replays are idempotent, and a delivery ID cannot be
reused for another effect. Exhaust the bounded pending queue using only
small evaluator-owned actions, show that a new action fails before effect,
lease, or approval authority, then acknowledge enough work to admit it.
Claim tokens appear only in the successful claim envelope.

Begin a format upgrade `1` to `2`, kill the evaluator-owned advance
process after only part of the request, and prove neither cursor progress
nor alternate authority became visible. Resume bounded progress, overlap one
compaction/key rotation and one fresh hook lifecycle, then catch up and
commit. Race two exact commit replays; both return the same commit. A
checkpoint branch created after the commit keeps format 2 and its upgrade
lineage, while checkpoint-only archives cannot advance the upgrade or claim
effects. Abort a separate building upgrade before commit and prove the exact
abort replay; post-commit abort and format rollback fail closed.

Bind the epoch-11 session to one continuation authority. Race two different
continuation IDs so that one pending exact-target token wins, replay that
issuance, and show that a policy successor's attempt and new source work
remain fenced while the token is pending. Activate one named target session
through concurrent exact Pre replays, refuse a sibling target, and continue
receipt/effect correlation across the incremented session generation
through key rotation and restart. A separate token must expire through
reconciliation and restore only its never-activated source.

After compaction, corrupt an internal compaction journal record. Hooks and
every maintenance command with authority must enter quarantine. Kill one
evaluator-owned partial repair advance, verify that no cursor was published,
then resume the bounded rebuild. Require the two distinct configured
verifiers to attest, refuse the reuse of one identity under another secret,
commit a rollback-protected repair generation, and recover the exact commit
output through replay. New epoch-11 work resumes only on the repaired
healthy owner. Checkpoint archives preserve repair/continuation evidence but
still cannot repair or activate, and all new views and raw durable state
remain bearer-free.

## Cycle-10 agent-loop public world (`assets/policy_agentloop_v3.json`)

The fixtures and assertions used by `run_public_case.py` are completely
unchanged. Cycle-10 additionally adds one **agent-loop public world**:
`assets/policy_agentloop_v3.json`, which puts the policy under
`mode: "audit_only"`. It examines the class of difficulty "the mode is
applied after all constraints and is final":

- `printf raw-status` hitting the exact rewrite rule,
  `curl https://blocked.test/x` hitting `deny_hosts`, a `.env` read
  hitting `paths.deny`, and `pwd` in `commands.audit_only` — in this mode
  **all four** return `permissionDecision=allow` and `decision=audit_only`,
  the deny that would have taken effect is written into
  `permissionDecisionReason`, and `updatedInput` is always JSON `null`
  (no rewrite);
- when the same policy is switched to `enforce`, the same four are
  respectively `allow/rewrite`, `deny/deny`, `deny/deny`,
  `allow/audit_only`;
- the `--audit` bounded view must exit with `0`;
- calling `--ack-effect` with an `effect_id` and claim token the ledger
  never issued must exit non-zero and change no state.
