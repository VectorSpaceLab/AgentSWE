# Evaluator instructions

The evaluator receives the current case input/assets, the three frozen
delivery files, the executed capsule and revision/execution responses, the
canonical harness evidence, this rubric, and the case checklist. Verify
delivery and artifact parseability before scoring. `run_report.json` must be
schema `1.0` with exactly three artifact names, a string-array errors field,
a non-negative top-level `runtime_seconds`, an integer `peak_memory_bytes`,
and integer `api_calls.gateway`, `.serper`, and `.web_retrieval`.

Run the canonical harness in fixed case order. Every assertion must be
supported by process responses, parsed files, checkpoint lists, digests, or
other behavior observable from that case. Do not infer from reports,
symbols, implementation architecture, prompts, or the source repository.

The Cycle 003 checklist dimensions are preserved behavior (12), immutable
revision review (8), resumable execution (8), cross-surface
promotion/recovery (12), isolation/compatibility (3), and operational
surfaces (57). The operational surfaces cover the append-only audit
ledger/export and the security quarantine/restore boundaries. Every hidden
checklist totals exactly 100.

For every valid case, score each explicit assertion according to
`rubric.md`, cite concrete evidence with operation/file locations, and record
deductions. Include the assertion ID, points earned, maximum points,
evidence, major errors, dimension subtotals, and the total. Keep behavioral
zeros and continue with subsequent cases after one case fails. Report the
arithmetic mean of the six integer case totals, without averaging
percentages or dropping failed cases.
