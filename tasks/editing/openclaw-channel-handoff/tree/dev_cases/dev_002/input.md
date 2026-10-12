# Developer case 002 — crash recovery, thread route, and media

You are operating a synthetic OpenClaw deployment with a fresh shared state
directory. A task is created through the legacy schema and migrated to the
current schema. A thread-routed user result includes a small attachment. A
provider accepts a media operation and the Gateway loses its response before
completion; a peer Gateway later recovers the operation and must finish it
without sending duplicate bytes or a duplicate channel message.

Drive the real OpenClaw Gateway agent through its production entry. Preserve
the exact thread route and stable effect/upload identities. Inspect only the
redacted state and receipts made available by the case client. Treat a partial
media receipt, an empty platform message identity, and connector acceptance as
insufficient for verified completion. Compression/restart must preserve
monotonic state and completed effects. Never put attachment bytes, provider
credentials, callback tokens, or internal payloads into the final artifact.

Write `agent_result.json` and `run_report.json` in the requested output
directory using the documented schemas. Record uncertainty honestly when the
available evidence cannot prove final delivery.


Write `agent_result.json` in the requested output directory as soon as you have
your first product response, and refresh it after every step. It carries the
case identifier, a concise decision, a `decision_state` of `complete` or
`partial`, the observed product actions, evidence references bound to this
rollout, and an explicit list of anything you did not attempt or could not
verify. Never record a step you did not run. `run_report.json` carries the same
case identifier, status, artifact paths, errors and basic usage statistics.
Connector acceptance is never verified delivery, and an operation you cannot
authorize from current durable state must be left alone rather than forced.

The evaluator also issues its own fixed battery of Gateway calls against this
task before your first turn, recorded with origin
`environment_invariant_probe`. Those calls are not your work and you are not
scored on them; they measure the product. `input/03_requirements_and_constraints.md`
requirement 21 lists exactly what they check.
