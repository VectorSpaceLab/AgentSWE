# Hidden case 006 — callback replay after compaction

Complete one verified button callback after a restart and compaction checkpoint
while a late duplicate provider event is present. Reconcile the event and
complete at most one durable interaction and one correlated reply on the
original route. A duplicate completion must replay existing state rather than
create another side effect. Report provenance and uncertainty without including
callback tokens, response bodies, or connector credentials.

Required end state: exactly one durable interaction is verified and completed,
exactly one correlated reply is accepted on the original source route, the late
duplicate provider event creates no second interaction, agent turn or reply, and
the compaction checkpoint resets no counter and drops no completed fact.

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
