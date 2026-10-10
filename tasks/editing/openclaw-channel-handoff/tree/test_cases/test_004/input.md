# Hidden case 004 — thread-route migration

Recover a legacy task originally created from a thread route. Migrate the task
to the current durable schema, then deliver its user result on that same thread
across restart or compaction. Migration must preserve, not rewrite, the original
provider/account/peer binding and session continuity. Reject foreign or stale
routes before channel I/O. Produce the required result and run reports without
exposing internal route structure or credentials.

Required end state: the task is a complete current-schema task whose original
provider, account, peer, thread and session binding are unchanged, exactly one
user result is accepted on that same thread route carrying the requested bytes,
and no duplicate or foreign-route message exists.

Write `agent_result.json` in the requested output directory as soon as you have
your first product response, and refresh it after every step. It carries the
case identifier, a concise decision, a `decision_state` of `complete` or
`partial`, the observed product actions, evidence references bound to this
rollout, and an explicit list of anything you did not attempt or could not
verify. Never record a step you did not run. Also write `run_report.json` with
the same case identifier, status, artifact paths, errors and basic usage
statistics. Connector acceptance is never verified delivery, and an operation
you cannot authorize from current durable state must be left alone rather than
forced.
