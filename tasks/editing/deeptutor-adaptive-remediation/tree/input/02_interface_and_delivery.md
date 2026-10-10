# Interface and delivery

The controller supplies four input documents, a writable copy of the pinned
repository, the two public dev descriptions, and a delivery directory. Work
non-interactively in one Builder session.

Read `05_mastery_tool_contract.md` as part of the public interface. The
Candidate must implement the complete named mastery surface there; preserving
only the original five mastery tools is an incomplete Candidate even if those
five tools compile and one public case can run.

Write exactly these top-level delivery files:

1. `solution.patch`: non-empty UTF-8 repository-relative unified Git patch.
2. `edit_report.json`: object with `schema_version`, `summary`, accurate
   `changed_paths`, and arrays `commands`, `compatibility_notes`, `limitations`.
3. `run_report.json`: exact schema below.

```json
{
  "schema_version": "1.0",
  "status": "success",
  "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"],
  "errors": [],
  "runtime_seconds": 0,
  "peak_memory_bytes": 0,
  "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0}
}
```

Submit a Candidate through the controller. The evaluator materializes the patch
and runs both dev cases using the patched DeepTutor mastery agent with the locked
lower model. Read the returned feedback, revise in the same Builder session,
regenerate all delivery files, and submit the next distinct Candidate digest
with the exact latest feedback digest. Up to ten accepted submissions are
allowed; infrastructure-invalid attempts do not consume a slot. The evaluator
freezes the latest accepted Candidate when the Builder exits or the maximum is
reached. Hidden cases run only after freeze.

## Lower-agent terminal artifact contract

The production mastery turn must support an evaluator-provided optional
`DEEPTUTOR_AGENT_RESULT` path. When that variable is present, the patched
DeepTutor product—not the evaluator launcher—must persist the exact final
model-authored JSON response to that path after the mastery turn completes.
The product may parse and atomically write the response, but it must not invent
content, fill missing fields, or replace malformed model output with a
controller-generated object. A valid response is one JSON object containing at
least `schema_version`, `case_id`, `status`, `summary`, and `artifacts`; the
`case_id` must match the case-local runtime identity supplied to that turn.
If the model does not produce a valid object, the product must leave the file
absent or report the malformed response rather than synthesizing a result.
This artifact is separate from raw StreamBus/tool trajectory evidence.
