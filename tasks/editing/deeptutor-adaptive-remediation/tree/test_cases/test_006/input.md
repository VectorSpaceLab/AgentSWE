# Hidden test 006 — stale witness head after new evidence

The learner has a previously audited witness head. New accepted evidence and a
new snapshot/witness append occur before reconnect. Reject the old audit head as
stale without rolling back or repairing the chain, verify the current head, and
ground the final remediation/review advice in the latest path-local lineage.
Finish with one JSON object containing `schema_version`, the runtime `case_id`,
`status`, `summary`, and `artifacts`.
