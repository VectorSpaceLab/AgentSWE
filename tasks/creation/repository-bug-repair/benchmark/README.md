# Repository Bug Repair Agent — hard v4

This Create-Agent benchmark measures whether a builder can produce a general repository-repair agent that diagnoses unfamiliar multi-module systems and emits a safe, evidence-backed patch. The eight synthetic Python repositories cover cache identity, durable JSONL migration, linearizable concurrency, SQLite schema migration, streaming performance, incremental protocol parsing, multi-tenant cache/transaction coherence, and torn binary segment recovery.

## Builder workflow

Give the builder exactly the four documents in `input/` plus the two `dev_cases/`. Do not provide `test_cases/`, `evaluator/`, `meta/`, or construction scripts. The builder owns its submission and the dedicated Conda prefix in `input/04_resources.md`.

Run each public case with a fresh output directory:

```bash
python run_agent.py --input <absolute-path>/dev_cases/dev_001/input.md --output <fresh-output-dir>
python run_agent.py --input <absolute-path>/dev_cases/dev_002/input.md --output <fresh-output-dir>
```

The active input's fenced `repair_contract` is machine-readable and authoritative for repository location, patch scope, public command, network policy, patch-size limit, and conditional recovery delivery. Assets are read-only. Each agent run has 600 seconds and 4 GiB.

## Hidden evaluation

Expose only one active hidden case to the created agent. Use a fresh output directory, then invoke:

```bash
python evaluator/harness/evaluate_case.py \
  --case-dir <active-case-dir> \
  --output-dir <agent-output-dir> \
  --result <harness-result.json>
```

The harness parses and validates the case contract, checks report schemas, validates patch text and size, copies the pristine repository, applies the patch once, derives the actual changed-path inventory, enforces allowed paths and symlink restrictions, runs the original public suite and evaluator-owned hidden tests under limits, and—when required—executes the declared recovery artifact from the pristine patched repository with an isolated work directory. Recovery succeeds only when the patched artifact reports all six phases and the evaluator's Python audit trace confirms format-relevant JSONL replacement/backup, repeated SQLite, or segment/manifest operations beneath that work directory. A print-only success object is rejected.

Run the harness inside a disposable OS sandbox: patched Python is executable code, and language-level checks are not a complete hostile-code boundary.

## Scoring

Provide the evaluator with the active input/assets, final artifacts, harness JSON, `evaluator/rubric.md`, and `evaluator/eval_prompt.md`. Score each hidden case independently out of 100 and report the arithmetic mean across six cases, retaining per-case gate and dimension results. The final-artifact result rubric and `evaluator/code_rubric.md` are separate 100-point axes and must never be averaged or substituted for one another.

## Benchmark validation

Regenerate public assets/inputs with the two scripts in `meta/`. Evaluator owners may separately regenerate hidden checks with `evaluator/harness/generate_hidden_tests.py`. Then run:

```bash
python3 -B evaluator/harness/validate_benchmark.py --run-tests
```

This checks exact counts, case-only runtime structure, contracts, syntax, dependency-free projects, 250–1200 physical production-line targets for hidden repositories, public/hidden hash separation, hidden package-name leakage, pristine public passes, and intended pristine hidden failures.
