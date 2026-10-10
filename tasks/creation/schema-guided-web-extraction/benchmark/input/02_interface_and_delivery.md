# Interface and Delivery

Create dependencies only in the dedicated environment described in `04_resources.md`. Run every request without interactive confirmation:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

`input.md` is UTF-8 Markdown. Resolve relative paths from the input file's directory. Multiple HTML, JavaScript, JSON, NDJSON, vCard, and PNG files may be referenced. Treat all case assets as read-only. Create the output directory if absent; write generated and temporary per-run data only inside it. On repeated runs, atomically replace all artifacts owned by the agent so stale records, evidence, traces, or screenshots cannot survive.

On success, exit 0 and write:

- `records.json`: a JSON array. Each object conforms to the active request's JSON Schema and contains no unrequested fields when `additionalProperties` is false.
- `evidence.json`: an object with arrays `sources`, `records`, `field_evidence`, `conflicts`, and `uncertainties`.
  - Each source has a stable `source_id`, authorized relative `path` or redacted loopback URL, `access_depth`, and content SHA-256 when locally available. In supplied local cases the hash is required. Allowed access depths are `local_file`, `browser_rendered`, `partial_page`, and `full_page`.
  - Each records entry has exactly one `record_id` naming a submitted stable identity. The set must equal the identities in `records.json`.
  - Each field-evidence item has `record_id`, JSON Pointer `field`, exact submitted `value`, `source_id`, nonempty `locator`, `access_depth`, `method`, and `action_id`. Allowed methods are `dom`, `json`, `vcard`, `ocr`, `join`, and `inferred`; unsupported inference must be represented as uncertainty, not fact.
  - OCR field evidence must use the actual PNG source, include a pixel-region locator such as `xywh=x,y,w,h` (an equivalent four-coordinate region is acceptable), and include either numeric `confidence` in `[0,1]` or a corresponding uncertainty entry.
  - Each conflict has a stable `conflict_id`, `record_id`, output-field JSON Pointer `field`, competing `observations`, exact selected value, nonempty reason, and deciding `action_id`. Each observation has `source_id` and `value`, plus its supplied timestamp, sequence, validity, or status when that attribute decides the result. Use separate conflict entries when one losing observation affects multiple output fields. Conflict IDs are candidate-chosen and are never compared to hidden evaluator names.
- `interaction_trace.json`: a nonempty ordered array. Every event has integer `seq`, unique `action_id`, canonical `action`, redacted authorized `locator`, nonempty `outcome`, resulting case-specified `state`, cumulative `counters`, and optional referenced screenshot path. Sequence numbers start at 1 and strictly increase by 1.
  - Canonical action values are `open`, `set_mode`, `load_more`, `visit_detail`, `read_local`, `ocr`, `merge`, `apply_update`, `parse_failure`, `checksum`, `apply_patch`, `policy_block`, `exclude`, and `state_check`. Use only actions that actually occurred; extra truthful diagnostics may use `state_check`.
  - A locator must identify the actual affected relative source path, control, image region, record identity, patch sequence, or prohibited target. Generic labels such as `step`, `source`, `record`, or `decision` are not evidence.
  - Counters contain nonnegative integers `local_reads`, `browser_navigations`, `gateway`, `gateway_image`, `serper`, `web_retrieval`, and `retries`; they are cumulative and may not decrease. Final trace counters must agree with `run_report.json`.
- `session_summary.json`: an object with `case_id`, `workflow_mode`, `completed`, `completed_states`, exact `reached_record_ids`, `excluded_observations`, and `limitations`.
  - `workflow_mode` is `browser` or `local_file`. Use the exact state IDs stated by the active case. Each excluded observation is either a stable source identity string or an object with an identifying value, nonempty reason, and linked `action_id`.
- `run_report.json`: an object with `status`, `artifact_paths`, `errors`, `usage`, `retries`, `elapsed_seconds`, `local_reads`, and `browser_navigations`. `usage` contains nonnegative integer counts for `gateway`, `gateway_image`, `serper`, and `web_retrieval`; `gateway_image` is included in `gateway`.
  - Artifact paths are relative paths inside the active output directory and must resolve to the files produced by that run.
- `screenshots/`: required only for browser or visual workflows. Files must be distinct, parseable PNG captures of the actual state or visual source named by the linked trace event. Blank, repeated, decorative, generated-placeholder, or unrelated source images are not screenshots. Static-only cases must not fabricate screenshots.

On failure, exit nonzero and write `run_report.json` with `status: "error"`, a nonempty secret-safe `errors` array, truthful counters, and no fabricated success artifacts. Never include credentials, raw provider payloads, hidden/evaluator data, private reasoning, or unrelated files.
