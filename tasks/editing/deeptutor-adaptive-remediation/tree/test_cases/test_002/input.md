# Hidden test 002 — concurrent review reschedule

Two clients observed the same review revision. One reschedule was accepted and
the other is stale. Preserve the accepted due time and event, do not duplicate
the task, and provide a safe recovery instruction for the stale client. Snapshot
and event checkpoints must remain independent. Finish with one JSON object
containing `schema_version`, the runtime `case_id`, `status`, `summary`, and
`artifacts`.
