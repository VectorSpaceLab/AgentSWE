# Public case: crashed claimed delivery across two instances

Implement the global contract and use `assets/scenario.json`. Two
independent adapter instances must share one backend-scoped ledger. Inject
a process crash at `after_claim`: the durable claim must be visible to the
second instance, the execute callback must not have run yet, and the live
claim must suppress the second delivery attempt without throwing.

After the claim expires, rebuild the service. Because the fixture is an
unsafe local write, recover through the stable idempotency key without
executing again. Keep `applied` and `completed`, then rebuild once more and
prove that the duplicate delivery ID/key is suppressed. Force one batch of
compaction and keep the scope's immutable revision set bounded.

In a separate scope, have an unsafe execute callback rejected after its
durable claim. The rejection must leave `uncertain` work with no live claim
before propagating. Rebuild and reconcile through the idempotency key
without executing the unsafe callback again.

In another scope, demonstrate the injectable durable cross-tab recovery
sync coordinator. The first tab durably claims transport leadership, loses
one pull response, and leaves the recovery cursor unchanged. After the term
expires, a new tab incarnation on the same storage takes over, pulls from
that exact cursor, and dispatches one accepted recovery event. Neither a
retry nor the late response may duplicate the ledger, ordinary events, or
the final effect. Channel notifications may echo to themselves and are
never treated as authoritative.

Also recover the workspace from an explicit common base manifest. The local
side adds `src/local.ts` while the remote side adds `docs/remote.md`. The
first content-addressed upload reaches the remote store but its response is
lost. A new reconciler must probe the remote, avoid uploading that chunk
twice, download and verify the remote-only chunk, commit the merged remote
manifest through the recovery ledger, apply the same manifest locally
through an exact revision, and finally publish the completion cursor. The
workspace status exposes counts and phase only. A separate same-path
double edit returns one conflict and performs no transfer, ledger callback,
apply, or cursor update.

Deliver `solution.patch`, `edit_report.json`, and `run_report.json`.
