# Hidden case 005 — attachment upload crash boundary

Resume a multi-block attachment after a lost upload response and a Gateway
restart. Use the provider's observed offset and stable upload identity. Do not
re-send already accepted blocks, do not downgrade a media result to text, and
do not verify the message until the exact media receipt set is complete. Keep
bytes and provider identities out of projections and final prose.

Required end state: exactly one logical provider upload completes, carrying the
exact ordered block set for the staged attachment, exactly one media-bearing
user result is accepted on the original source route, and no duplicate upload,
duplicate message or text-only downgrade exists.

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
