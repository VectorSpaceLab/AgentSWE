# Deep Evidence Research Agent Benchmark V2

## What This Benchmark Measures

This Create-Agent benchmark tests whether a coding agent can build a reusable deep-research agent whose conclusions survive an evidence audit. The created agent must move beyond search snippets: refine queries, paginate when useful, inspect full pages and PDFs, traverse relevant links, resolve entities and dates, distinguish current from superseded material, verify claims across independent evidence, analyze conflicts and provenance, recompute public data, recover from degraded sources, and disclose access depth.

The observable output is a decision-useful Markdown report plus structured source, claim-passage, calculation, and runtime records. Evaluation accepts different conclusions when the final artifacts make the evidence and decision logic supportable.

## Builder Package

Give the tested coding agent these four documents together, in order:

1. `input/01_task_goal.md`
2. `input/02_interface_and_delivery.md`
3. `input/03_requirements_and_constraints.md`
4. `input/04_resources.md`

Expose `dev_cases/` during development. Do not expose `test_cases/`, `evaluator/`, `meta/`, hidden-case filenames, or prior hidden outputs to the builder or created agent.

## Environment Setup

The builder creates and owns this dedicated prefix:

```bash
conda create -y \
  -p /opt/agentswe/benchmark/envs/web-research-report-agent-v2 \
  python
```

The builder selects all libraries and browser/parser dependencies. Load provisioned credentials without printing them:

```bash
eval "$(conda shell.bash hook)"
conda activate /opt/agentswe/benchmark/envs/web-research-report-agent-v2
set -a
source /opt/agentswe/benchmark/envs/.env
set +a
```

## Development Runs

Run each public case with the same command and a fresh output directory:

```bash
python run_agent.py \
  --input /absolute/path/to/dev_cases/dev_001/input.md \
  --output /absolute/path/to/run_outputs/dev_001
```

A successful run produces `report.md`, `sources.json`, `evidence_graph.json`, and `run_report.json`. The harness enforces 600 seconds and 4 GiB per case. Search and public retrieval have no benchmark-imposed call, result, page-byte, redirect, or per-request timeout cap inside that global envelope.

## Hidden-Test Isolation

There are exactly six hidden tests. During evaluation, mount or copy only one active case into a clean runtime workspace. Do not expose sibling hidden tests, evaluator files, construction metadata, development outputs, or earlier hidden outputs. Use the same Conda prefix, credential injection, command, resource limits, and network policy as development, and use a new empty output directory for every run.

## Mechanical Inspection

Before qualitative scoring, the harness should:

- Confirm exit status and runtime/memory compliance.
- Decode `report.md` as nonempty UTF-8 Markdown and optionally render it.
- Parse all three JSON artifacts.
- Validate the declared schemas, ID formats, enum fields, and unique IDs.
- Cross-reference every report `[C#]`, claim support/contradiction link, source ID, passage ID, source-passage ownership, and calculation input.
- Confirm that a `search_snippet` source supplies no graph passage.
- Verify web locator syntax, local-path containment, artifact and usage counts, and the case's minimum inspected-source/body requirements.
- Recompute reported calculations and rounding or sensitivity bounds when practical.

Mechanical success does not prove that a quote, locator, access-depth label, source relationship, or conclusion is truthful. Those require independent evidence checks.

## Evaluation

For each hidden case, give the evaluator:

- The active `input.md` and any case assets.
- The created agent's final four artifacts.
- Harness parse/render/cross-reference/recomputation results.
- `evaluator/rubric.md`.
- `evaluator/eval_prompt.md` as the instruction.
- Its own isolated Search/Scrape capability for consequential-claim verification.

The evaluator must not inspect the submission's implementation, prompts, diagnostics, private reasoning, or tool trajectory. It should independently retrieve a risk-based sample of cited sources and relevant alternatives to verify source access depth, exact passages, dates, supersession, dependence, contradictions, and calculations.

## Score Aggregation

Apply the shared 100-point rubric independently to each of the six hidden cases. A case meeting a zero-score execution or validity condition contributes `0`. The benchmark score is the arithmetic mean of the six totals, reported to one decimal place. Retain per-case dimension scores, evidence for deductions, major errors, and evaluator verification notes.
