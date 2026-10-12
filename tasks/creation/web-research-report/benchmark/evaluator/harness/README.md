# Evaluator Protocol v1 Harness

The harness separates execution health from research quality and keeps evaluator-only facts outside runtime cases.

For each fresh case, the evaluator performs:

```bash
python evaluator/harness/run_case.py \
  --python <ENV_PREFIX>/bin/python \
  --submission <SUBMISSION_DIR> \
  --case-input test_cases/test_001/input.md \
  --output <RUN_DIR>/output \
  --evidence-dir <RUN_DIR> \
  --env-file <SHARED_ENV_FILE>

python evaluator/harness/evaluate_case.py \
  --case-input test_cases/test_001/input.md \
  --output-dir <RUN_DIR>/output \
  --evidence-dir <RUN_DIR>
```

`run_case.py` probes GATEWAY and Serper without persisting credentials or response bodies, then invokes the candidate once under the 600-second/4-GiB limits. `evaluate_case.py` validates artifacts, classifies the run, and creates a body/PDF verification plan only for complete successful artifacts.

`infrastructure_invalid` requires matching candidate and independent probe evidence. It is unscored and must be rerun. Publisher-specific access failures, candidate timeouts, malformed requests, and unsupported self-reported errors remain candidate behavior.

Validate the package and protocol locally with:

```bash
python evaluator/harness/validate_package.py --benchmark .
python -m unittest discover -s evaluator/tests -p 'test_*.py' -v
```
