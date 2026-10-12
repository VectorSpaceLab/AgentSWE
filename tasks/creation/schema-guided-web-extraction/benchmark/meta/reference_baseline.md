# Reference baseline

The upstream implementation was not cloned or executed. This benchmark is implementation-independent and uses evaluator-only source-grounded bundle construction for validator regression rather than treating a source-repository output as the sole correct artifact.

`evaluator/make_reference_bundle.py` builds disposable contract-shaped bundles from `evaluator/oracles.json`, `evaluator/case_truth.py`, and real case asset hashes. It exercises every required record, accepted field source/method/action link, semantic conflict, action target, state, exclusion, OCR region, screenshot linkage, counter, and case budget. Candidate-chosen action and conflict IDs remain unconstrained.

The evaluator tooling requires Python 3.10 standard library. The construction audit optionally uses an available Node executable for JavaScript syntax checks. Deterministic validation requires no network credentials, browser binary, OCR package, or model call. Candidate agents may use the declared browser/OCR/model resources during benchmark runs.

## Validation record

Validation was performed on August 24, 2026.

- `python3 evaluator/audit_benchmark.py` checks every runtime file, local HTML reference, case schema, raster CRC/dimensions/metadata boundary, JavaScript syntax, source-grounded field rule, conflict observation, required action/state, public budget, hidden/public hash boundary, OCR text separation, rubric total, shared code-rubric ID, and release-hygiene condition.
- `python3 evaluator/test_validator.py` accepts all eight source-grounded bundles, accepts candidate-chosen conflict IDs and documented loopback source URLs, and rejects mutations for wrong identity/schema/source/action linkage, placeholder action locators, fabricated conflicts, missing evidence-record edges, wrong state transitions, duplicate/copied screenshots, bad OCR method/region, missing exclusions, wrong patch order, static screenshots, and work-budget violations.
- Evaluator-generated bundles for all eight cases are also passed individually through `evaluator/validate_case.py` from disposable system-temporary directories.
- Representative quality-lot and archive-mark PNGs were visually inspected. Both contained legible rasterized text and no textual/metadata surrogate; automated corpus checks verify that every OCR-scored truth is absent from non-PNG runtime files and filenames in its active case.

No generated bundle is retained in the benchmark tree.
