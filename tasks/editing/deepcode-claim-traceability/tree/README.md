# DeepCode reviewable revisions and recoverable execution Edit Agent-loop sibling

This directory is the `owner-17` migration sibling. The authoritative source is at `@@AGENTSWE_EDITING_SOURCES@@/17-edit-deepcode-claim-traceability`; the original directory, 0830 staging, and old formal runs are read-only.

Current state: `PARTIAL_STATIC_ONLY`. This sibling implements a real DeepCode lower-agent entry point, an evaluator-owned broker, Candidate materialization/build, up to ten dev-feedback rounds and freeze, a canonical six-case post-freeze hidden executor, per-case evidence, the same-session Builder protocol, the Agent-loop Result rubric, and an independent Code rubric. Local fake-launcher six-case smoke, 16 harness unit tests, and `py_compile` pass; no real Builder or formal provider run has started, so no real successful broker calls exist. Static/local smoke is not a formal Result. See the migration record for details.

The real lower-agent command is assembled by `evaluator/harness/deepcode_lower_agent.py` as `deepcode.py exec ... --json` against the patched repository, with an isolated `DEEPCODE_HOME`. Launcher, configuration, and broker lock the model and effort to `gpt-5.6-sol` / `medium`; the real credential is read only by the broker, and the Candidate receives a placeholder.

Protocol lock, source digest, hidden inventory, freeze schema, and infrastructure classification are in `evaluator/hidden_inventory.json`, `evaluator/schemas/`, and `infra_classification.json`. `python3 tools/self_test.py` runs the lightweight stage-A self-test; it does not start Docker, a provider, or a formal evaluation.

This Agent Edit benchmark measures whether a Builder can preserve DeepCode's portable claim-to-code capsule and durable publication boundary while adding four independent production surfaces: immutable semantic-revision review, checkpoint-resumable execution, append-only audit export, and safe isolation/restore. Hidden evaluation emphasizes multi-participant policy, response-loss recovery, concurrent operations, corruption, cancellation fences, and atomic promotion of both surfaces.

## Builder package

Provide only this README, all four documents under `input/`, a writable byte-for-byte copy of `input/repository`, and all `dev_cases` (specifically `dev_001`, `dev_002`, and `run_public.py`). Do not expose `test_cases`, `evaluator`, `meta`, prior submissions, or evaluation history.

The source is fixed at `69233821b5dbcf044eb17f91bca1b9c6b1d2fda5` on `main`, licensed under MIT, and copied without Git history. The Builder delivery is exactly `solution.patch`, `edit_report.json`, and `run_report.json`; public and hidden prechecks enforce the shared top-level delivery schema before applying the patch.

## Public development

From the benchmark directory:

```bash
python dev_cases/run_public.py \
  --submission <submission_dir> \
  --case dev_001 \
  --json-output <result.json>
```

Use `dev_001` for nominal registration, semantic differences, dual-role review, checkpoint execution, and promotion. Use `dev_002` for pause/resume, response-loss retry, changed-comment generation, and stale-generation blocking. The public runner copies the original source, validates and applies the patch once, runs focused upstream gates, and then executes only the selected public case. It prints named behavioral assertions and bounded process evidence.

The patched product exposes four offline CLIs:

```bash
python -m workflows.traceability --request <request.json> --output <capsule>
python -m workflows.traceability_runs --store <store> --operation <operation.json>
python -m workflows.traceability_revisions --store <store> --operation <operation.json>
python -m workflows.traceability_execution --store <store> --revision-store <store> --operation <operation.json>
```

Revision commands also accept `audit`, `quarantine`, and `restore`. Audit is a tenant/project-scoped hash chain; quarantine blocks new execution and promotion until an authorized, digest- and generation-isolated restore is completed.

## Driveability

The upstream agent sees only the patched repository. The capsule must be self-describing (the case identifier appears in its own graph, `paper_spec.json`, or mapping), every new command's `--help` must list its operations and required fields, and the Agent-loop result contract must be recorded inside the product. Hidden cases require revision review, resumable execution, audit export, and isolation/restore as actual product behavior; preserving only the capsule and durable-run surfaces cannot pass hidden evaluation.

## Hidden evaluation

Keep all six `test_cases` and all `evaluator` material isolated. The canonical runner prepares one Candidate and evaluates six independent temporary projects at fixed roots:

```bash
python evaluator/harness/run_hidden.py \
  --submission <submission_dir> \
  --json-output <hidden_result.json>
```

Each Candidate subprocess has a 600-second timeout. Linux process-tree RSS is sampled with sufficient headroom; virtual-address limits are not used. Credentials are removed and the agent points at a closed local endpoint. A case failure does not prevent later cases from running.

Each evaluator-owned rubric totals 100 points: retained behavior 12, revision review 8, breakpoint-resumable execution 8, cross-surface promotion/recovery 12, isolation/compatibility 3, and two new operational surfaces 57. Missing behavior is a behavior zero rather than a build/setup failure. Case-local caps apply when cross-tenant disclosure, post-cancellation stale execution, or promotion without established review and execution authority is observed.

The suite reports the arithmetic mean of six integer case scores. Include behavior zeros; do not discard cases, average percentages by size, or infer credit from reports or source symbols.

## Evaluation procedure

For each case provide its inputs/assets, three frozen delivery files, observable capsule/revision/plan responses and artifacts, the canonical harness JSON, the evaluator-owned rubric `evaluator/rubric.md`, and `evaluator/eval_prompt.md`. Validate artifact parseability first, then score every dimension from executed evidence and cite each inference.

## Benchmark self-validation

Run:

```bash
python -m compileall -q evaluator dev_cases/run_public.py
python -m unittest -v evaluator.tests.test_harness
python evaluator/tests/run_calibration.py
```

The suite checks the exact case count, inventory, shared delivery schema, source-immutability metadata, evaluator/public-package isolation, clean case directories, and non-vacuous behavior scores.
