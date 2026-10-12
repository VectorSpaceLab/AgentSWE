# Hidden test 005 — reset/redo lifecycle

An open remediation, review, lease, and handoff cross a legitimate reset/redo
boundary. Verify that terminal provenance is preserved, stale acknowledgements
cannot revive work, and later evidence creates a new lineage rather than
reusing the old snapshot proof. Recommend the next learner action from the new
accepted state only. Finish with one JSON object containing `schema_version`,
the runtime `case_id`, `status`, `summary`, and `artifacts`.
