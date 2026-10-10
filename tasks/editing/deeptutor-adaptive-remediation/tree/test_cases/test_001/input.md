# Hidden test 001 — lost claim response and lease takeover

Reconcile a remediation claim whose first response was lost. Determine the
current owner/generation from product state, avoid a dangerous duplicate claim
before expiry, permit a fenced takeover after expiry, and reject a stale
acknowledgement. Ground the final learner plan in the surviving review,
event-feed, and snapshot provenance. Finish with one JSON object containing
`schema_version`, the runtime `case_id`, `status`, `summary`, and `artifacts`.
