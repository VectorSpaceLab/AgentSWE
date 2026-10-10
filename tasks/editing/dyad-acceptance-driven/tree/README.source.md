# Dyad acceptance-driven development Edit benchmark v2

This Cycle 002 Edit benchmark asks a coding agent to preserve Dyad's acceptance-development loop and durable recovery/cancel transactions, then add two question-supported production surfaces: user-visible acceptance-test execution in the preview and durable revision test evidence. Only a current, complete, workspace-matching preview proof may become acceptance evidence.

## Builder package

Provide only `input/`, one writable fixed repository, two `dev_cases/dev_*` requests, and `dev_cases/assets`, `public_harness.py`, and `run_dev_case.py`. Do not provide `test_cases/`, `evaluator/`, `meta/`, prior submissions, or evaluation results.

The delivery consists of exactly three ordinary files: `solution.patch`, `edit_report.json`, and `run_report.json`. Public and hidden prechecks require schema `1.0`, defined in `input/02_interface_and_delivery.md`, with top-level `runtime_seconds`, `peak_memory_bytes`, and `api_calls` fields.

The repository is an unchanged archive of the `dyad-sh/dyad` commit `c7896a72e4ceaa14410832e53334d15a97e7d70f`.

## Public development

Prepare dependencies once in `input/repository`, or let the runner install from the lockfile, then run:

```bash
python dev_cases/run_dev_case.py \
  --case dev_001 \
  --repository input/repository \
  --submission /path/to/submission \
  --work-root /tmp/dyad-acceptance-public \
  --result /tmp/dyad-acceptance-public/dev_001-result.json
```

`dev_001` demonstrates recovery from a lost pass preview and durable proof across a started response/retry and database reinitialization. `dev_002` demonstrates honest infrastructure/cancellation, invalid workspace revisions, and ordinary test compatibility. Public assertions explain behavior without disclosing hidden schedules or outputs.

## Hidden evaluation

The canonical evaluator isolates exactly six cases:

```bash
python evaluator/harness/evaluate_suite.py \
  --repository input/repository \
  --submission /path/to/submission \
  --work-root /tmp/dyad-acceptance-hidden \
  --result /tmp/dyad-acceptance-hidden/result.json
```

Dependency mode defaults to `auto`; `prepared` forbids installation and `install` forces it. The patch is applied once to a temporary copy. Type and focused compatibility gates run once, then each case runs in a fresh process with a private state root, a 600-second timeout, and 8 GiB recursive RSS headroom, without virtual-address limits or non-loopback network access.

Run local benchmark validation with:

```bash
python evaluator/harness/selftest.py
python evaluator/harness/evaluate_suite.py \
  --baseline --repository input/repository \
  --work-root /tmp/dyad-acceptance-baseline
```

The self-test verifies inventory, delivery/schema rejection, the 100-point rubric, complete and incomplete controls, case-local caps, resource monitoring, and Builder-package isolation. Missing baseline behavior is valid zero evidence; setup/build/probe failure remains invalid.

Cycle 002 calibration records `[100, 100, 100, 100, 100, 100]`, a Cycle 001-complete control of `[15, 15, 15, 15, 15, 15]`, a nominal public-surface-only control of `[25, 25, 25, 25, 25, 25]`, and six valid zero-score fixed-baseline observations. The canonical runner records fixed-snapshot manifest hashes before and after the suite and rejects drift. Recursive RSS is measured per process tree under an 8 GiB cap without virtual-address limits.

## Evaluator handoff

Use `evaluator/eval_prompt.md`, the canonical result, and `evaluator/rubric.md`. Score only patched-product behavior. Report all six case scores and their exact arithmetic mean; do not infer delivery claims or expose hidden fixtures.
