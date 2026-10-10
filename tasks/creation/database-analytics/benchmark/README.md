# Database Analytics Agent Hard v4 Benchmark

This Create-Agent benchmark measures whether a coding agent can build a trustworthy SQLite analytics agent whose final outputs generalize across large synthetic workspaces. V4 raises analytical difficulty through interacting effective definitions, full-replacement corrections, payment/refund fan-out, local time and DST, mixed units/currencies, permission-before-access, deterministic primary plus complementary suppression, whole-source recovery, and evidence-inferred insufficiency.

The reference repository is feasibility evidence only. The benchmark does not require or reward reproducing its architecture.

## Builder setup

Provide the agent builder exactly the four documents under `input/`, in order:

1. `01_task_goal.md`
2. `02_interface_and_delivery.md`
3. `03_requirements_and_constraints.md`
4. `04_resources.md`

The builder owns its submission and dedicated Conda prefix at `envs/database-analytics-agent-hard-v4`. During development it may inspect and run only `dev_cases/dev_001` and `dev_cases/dev_002`. Do not expose `test_cases/`, evaluator code, construction metadata, evaluator computations, or hidden-run output.

## Public development runs

After loading the provisioned environment variables and activating the created environment, invoke:

```bash
python run_agent.py \
  --input database-analytics-agent-hard-v4/dev_cases/dev_001/input.md \
  --output <fresh-output-dir>
```

Repeat with `dev_002`. Resolve assets relative to the active `input.md`, treat the entire case directory as read-only, and use a fresh output directory or the contract's safe repeated-run behavior.

The public cases calibrate the artifact contract and two core workflows:

- a large versioned commerce bridge with local periods, corrected lines/refunds, fan-out protection, mixed currencies, and dependent ranking/decomposition;
- permission-before-access plus explicit primary/complementary suppression while retaining safe plan margins.

They do not reveal hidden schemas, query skeletons, time-series structures, recovery layouts, or insufficiency keys.

## Hidden evaluation

Run `test_001` through `test_006` independently. Each run receives only its active `input.md` and referenced assets. Isolate process, output, counters, logs, and environment observations; prohibit listing or reading sibling hidden cases.

Every authored case is closed-local. `serper` and `web_retrieval` counts must be zero. The uniform command is:

```bash
python run_agent.py --input <case/input.md> --output <case-output-dir>
```

Enforce the 600-second/4-GiB envelope and provider budgets from `input/04_resources.md`.

## Evaluation

For one active hidden case, provide the evaluator:

- that case's `input.md` and referenced runtime assets;
- the isolated final output directory;
- harness observations for launch/exit, elapsed time, memory, API/network calls, filesystem writes, and unchanged case/database hashes;
- `evaluator/rubric.md` and `evaluator/eval_prompt.md`;
- deterministic parse/re-execution output and an offline dashboard render/interaction result.

Run the oracle-free validator first:

```bash
python database-analytics-agent-hard-v4/evaluator/validate_artifacts.py \
  --case-input <case/input.md> \
  --output <agent-output-dir>
```

It checks the public artifact schemas, declared control IDs, offline HTML, read-only SQL re-execution, exact query-CSV fidelity, result/chart equality, decision/lineage references, artifact declarations, and closed-local counters. It contains no expected business values or per-case query plan. The evaluator independently computes semantic results from the active permitted assets.

There is no active `dashboard_replay.json` contract, hidden dashboard JavaScript API, transition transcript, or case-specific evaluator contract. Dashboard behavior is judged only from public case requirements and rendered final-artifact consistency.

Score each hidden case independently with the one 100-point final-artifact rubric. Report all six scores and their arithmetic mean. Keep the unrounded mean; an optional display may round to two decimals. A zero gate applies only to the affected run.

The separate `evaluator/code_rubric.md` scores the immutable implementation on eight dimensions totaling 100. Do not average the code score with result scores unless an outer benchmark explicitly defines such an aggregation.

## Released contents

- Exactly four builder input documents.
- Exactly two public development cases.
- Exactly six structurally distinct hidden cases.
- Eight synthetic SQLite databases with hundreds to several thousand fact rows per analytical workspace, plus one authorized CSV fallback.
- One final-artifact-only 100-point rubric, one evaluator prompt, one eight-dimension 100-point code rubric, and one generic oracle-free artifact validator.
- Source analysis, baseline record, and construction report.

Case directories contain only runtime `input.md` and referenced assets. They contain no oracle, reference answer, required-content list, hidden rubric, judge context, generator, temporary output, or compiled cache.
