# Developer case 001 — direct-route handoff and callback

You are operating a synthetic OpenClaw deployment with two Gateway processes
sharing one isolated state directory. A user starts a task from a direct
channel route and grants a second channel reduced access. The first Gateway
then loses its lease. Continue the task from the second Gateway, deliver one
user-visible result through the configured local channel connector, and handle
one button callback associated with that result.

Use only the Gateway RPC contract exposed by the running OpenClaw product and
the case-local connector actions. Preserve the exact provider/account/peer and
optional thread route, use idempotency keys for retries, and distinguish
connector acceptance from verified delivery. If an operation is ambiguous,
reconcile its receipt before retrying. Do not expose grants, callback tokens,
connector credentials, or internal control payloads in projections or the final
answer.

Write `agent_result.json` in the requested output directory. It must contain a
case identifier, a concise decision, a list of observed product actions, a
list of evidence references bound to the current rollout/state, and an explicit
accounting of any unknown or partial outcome. Also write `run_report.json` with
status, artifact paths, errors, and basic usage statistics. Do not include
private reasoning or hidden evaluator data.


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
