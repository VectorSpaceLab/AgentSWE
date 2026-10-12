# Task goal: cross-instance transactional effect delivery

Extend the supplied OpenHands Agent Canvas repository with a backend-scoped,
versioned recovery ledger for conversation effects. The product must stay
honest when two service instances or browser tabs share storage, a process
stops after any delivery stage, a lease expires and moves to a new runtime,
or pause/resume/cancel races an old callback.

The ledger is a transactional outbox, not merely a history snapshot. It
durably enqueues an effect, claims it under a lease fencing token and a run
generation, records its callback return, and settles it. A new instance
must decide from durable state whether to suppress, replay, reconcile,
complete settlement, or block. A callback must not run before its claim is
readable from another instance, and a callback result cannot settle after
the lease, claim, delivery attempt, run generation, workspace, or terminal
authority has become stale. When a successor reconciles uncertain work
while the original callback is still suspended, the successor's completed
revision is authoritative, and the late original result is an audit-only
suppressed observation. Multiple in-flight deliveries under one lease keep
independent delivery and claim identities: a takeover, recovery, or
out-of-order late result for one must not corrupt or settle the others. A
rejected execute or reconcile callback must first leave an honest durable
recovery state so that a restart does not mistake the exception for
unattempted or completed work.

Recovery state is scoped by `backendId` and `conversationId`. Same-named
conversations on different backends cannot see or affect each other.
Status and support inspection use expiring, least-privilege grants issued to
browser tabs. Cross-tab storage notifications must converge the recovery
store and visible status without exposing effect bodies, credentials, grant
tokens, claim tokens, or another backend's state. Replacing a watched grant
must remove the previous listener before the new watch becomes current;
late notifications from the previous watch cannot overwrite the new grant's
projection. A replayed runtime event ID must suppress every optional
ordinary-event side effect, even when the replay arrives in a freshly
constructed wrapper with a different ordinary UI event ID.

Route REST history preload and live WebSocket recovery traffic through one
recovery-aware conversation event dispatcher. A reconnect opens a
replacement session from the durable checkpoint cursor instead of
requesting or applying the full history again. Late callbacks from a
replaced session cannot write to the ledger or any store/terminal/UI sink,
and a history/socket echo may produce terminal output and ordinary UI
events only after the recovery commit is accepted.

Coordinate recovery transport across browser tabs and page lifecycles. For
every backend/conversation scope, an injectable durable sync coordinator
elects one transport-pull leader under a browser-local short-lived lease. A
frozen tab, a restarted tab incarnation, or a tab that lost a transport
response can be replaced without skipping the durable event cursor or
applying an old response. Broadcast-style notifications are hints only:
every pull and every response re-validates the durable term/incarnation
authority before invoking the dispatcher or any sink. The coordination
records in notifications and storage are exactly scoped, bounded, and
contain no recovery envelopes, ordinary events, terminal text, credentials,
grants, claims, or fencing tokens.

Reconnection must also recover the workspace itself, not only the event
history and effect metadata. Add an injectable workspace reconciler that
compares an immutable content-addressed base, local, and remote manifests.
It may transfer only verified missing chunks, and may apply only a
conflict-free three-way plan. It must never overwrite a path that changed
differently on both sides, infer a common ancestor, trust an advisory
remote head, or publish the recovery cursor before both workspace copies
converge on the same manifest.

Workspace reconciliation is a recoverable transaction. Its authority is the
full cross-tab sync term/tab incarnation plus the ledger run generation. A
lost response after a chunk upload or a remote commit, a process restart at
any stage, or a takeover while an old commit is suspended cannot commit
twice, apply an old manifest, or advance the event cursor. The remote
manifest commit is a recoverable ledger effect; the local apply uses an
exact pre-image revision; and the `workspace_reconciled` runtime event is
committed last, so the dispatcher and sync resume from a cursor that
describes a fully converged workspace. Public status projections expose
only bounded counts and phase codes, never paths, file contents, chunk
bytes, content digests, credentials, or authority tokens.

The core value is exactly-once settlement with at-least-once recovery
decisions: side effects after a crash may be uncertain, so a replay without
reconciliation is unsafe. Keep ordinary conversations without checkpoints,
the existing event ordering/de-duplication, the existing proxied request
paths, and the existing conversation/runtime APIs.

Do not add a remote database, a server-side coordinator, provider/model
calls, real container control, or a replacement conversation backend.
Browser `localStorage`, synchronous immutable revision writes,
`sessionStorage` tab identity, injected BroadcastChannel-like notifications,
injected recovery transport pulls, injected workspace/chunk adapters, and
injected remote manifest commits are the deterministic production
boundaries of this benchmark. No live WebSocket, filesystem traversal,
object storage, or browser automation is required.
