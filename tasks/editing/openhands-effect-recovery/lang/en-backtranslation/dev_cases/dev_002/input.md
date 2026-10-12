# Public case: lease fencing, tab authority, and UI convergence

Implement the global contract and use `assets/scenario.json`. Two backend
instances contend for an expired lease on the same revision; exactly one
obtains a new epoch, fencing token, and run generation. The old instance
and the losing contender cannot enqueue or settle work.

Under the winning lease, issue a status-only inspection grant to a second
browser tab. Prove that the same token from the wrong tab and the same
conversation ID on another backend are isolated. Through the production
conversation/runtime service, the recovery store, and `RecoveryStatus`,
watch the granted scope. Complete one recoverable effect from another
instance, dispatch the matching browser storage notification, and observe
bounded UI and store convergence without a manual refresh. An unrelated
backend notification must not change the watched state. Ingest one runtime
recovery event through the production event store, then replay its ID; the
ledger and the optional UI event side effects must each advance only once.
The replay carries a different ordinary UI event ID, so suppression must use
the durable recovery event ID rather than ordinary store de-duplication.

Open a recovery-aware conversation event session before reconnecting, then
replace it with a new session using the same caller session ID. The
replacement starts at the durable checkpoint cursor. A late high-cursor
event from the old session is suppressed before any ledger/store/terminal
call, and closing the old handle cannot close the replacement handle. Send
one history event, echo it through the socket with a different ordinary
wrapper, and prove that the ledger, the ordinary event sink, and the
terminal output sink each advance once. A later session resumes at the
accepted durable cursor rather than the full history.

Also open an unsafe production runtime callback after lease expiry. Let a
new runtime take over and reconcile that delivery to completion before the
old callback returns. The old promise must settle suppressed; it cannot
replace the successor's authority or the completed delivery, and must leave
stale-settlement audit evidence.

Replace an active recovery store watch with a new grant for the same tab
and scope. A late matching storage notification for the old watch must be
processed only under the replacement request and cannot remove or deny its
visible summary.

Finally use the production `ConversationService` sync coordinator factory.
Start one transport pull, freeze that tab past its term, and restart with
the same tab ID but a new incarnation. The successor must take over the
ledger cursor and dispatch the response once. Releasing the old response
afterwards returns a stale-leadership observation before the production
dispatcher, recovery store, ordinary event sink, or terminal sink is called.
Duplicate, late, or wrong-backend channel notifications cannot grant
authority or change the successor projection.

Finally use the production workspace reconciler factory. A local-only file
is uploaded and the remote manifest CAS succeeds, but its response is lost.
The first run leaves an honest uncertain ledger effect and does not apply
the target locally or publish its completion cursor. A new production
reconciler proves the already committed target through `reconcileCommit`,
applies the same manifest locally through the exact pre-image, publishes
`workspace_reconciled` last, and lets a newly opened dispatcher session
resume from that cursor. The remote manifest commit happens once.

Deliver `solution.patch`, `edit_report.json`, and `run_report.json`.
