# Schema-Guided Web Extraction Agent V4 Benchmark

This Create-Agent benchmark measures whether a coding agent can build a reusable, auditable extractor for closed-corpus web and local-source workflows. V4 emphasizes stateful completeness, identity resolution, temporal/source reconciliation, visual extraction, deterministic degraded-resource recovery, and field-to-action provenance at realistic small-batch scale.

## Builder package

Give the tested Builder only:

- `input/01_task_goal.md` through `input/04_resources.md`;
- `dev_cases/dev_001` and `dev_cases/dev_002`.

Do not expose `test_cases/`, `evaluator/`, or `meta/`. The Builder writes its implementation in a separate submission directory. The benchmark cases never require access to this benchmark's source repository or evaluator-only oracle/reference tools.

The created agent is invoked uniformly:

```bash
python run_agent.py --input <absolute-case-input.md> --output <fresh-output-dir>
```

A successful run writes `records.json`, `evidence.json`, `interaction_trace.json`, `session_summary.json`, and `run_report.json`. Browser or visual workflows also write referenced PNG evidence under `screenshots/`. The output directory is the only per-run write location.

## Public development loop

Run both public cases independently with fresh output directories. `dev_001` demonstrates 24-entity, four-state catalog traversal, three detail templates, temporal feed selection, inventory reconciliation, and losing-observation disclosure. `dev_002` demonstrates 20-person identity resolution across four states and three profile templates, including missing directory IDs, homonyms, aliases, and a physically late but stale duplicate.

The evaluator-side deterministic validator can be used by benchmark operators:

```bash
python evaluator/validate_case.py \
  --case-dir dev_cases/dev_001 \
  --output-dir <candidate-output> \
  --result <validator-result.json>
```

Do not give the validator or oracle to the Builder.

## Hidden evaluation

Keep all six hidden cases isolated until each invocation. Use a fresh output directory per case, enforce 600 seconds and 4 GiB, preserve process/network observations, and never expose artifacts from another case. Hidden cases contain only their runtime `input.md` and assets; no case-local oracle or scoring file exists.

For result evaluation, provide the evaluator with the active case request/assets, candidate final artifacts, necessary parser/render/process observations, `evaluator/eval_prompt.md`, and `evaluator/rubric.md`. Apply the validity gate first, then score all six final-artifact dimensions. The benchmark result score is the arithmetic mean of the six hidden integer totals, including zero execution failures, rounded once to one decimal place.

The implementation score is separate. Apply `evaluator/code_rubric.md` to immutable candidate source and the four public input documents only. Do not average the 100-point code axis with the 100-point result axis.

## Construction and leakage checks

`evaluator/audit_benchmark.py` verifies counts, local references, schemas, PNG integrity, JavaScript syntax availability, provenance boundaries, public/hidden hash separation, public budget disclosure, code-rubric IDs, and source-grounded evaluator truth. `evaluator/case_truth.py` derives evaluator-only field/source, action-target, state-order, conflict, exclusion, OCR, and recovery requirements from the immutable case assets. `evaluator/test_validator.py` creates disposable evaluator-only bundles under the system temporary directory and applies compatibility plus adversarial mutations.

All runtime data and raster labels are newly authored synthetic material. Raster-only label truths are not duplicated in runtime HTML, JSON, filenames, alt text, or metadata.
