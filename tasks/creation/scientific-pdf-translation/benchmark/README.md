# Interactive Scientific PDF Translation Agent Benchmark

## What This Measures

This Create-Agent benchmark evaluates whether a coding agent can build a reusable system that translates scientific PDFs faithfully and produces an auditable local reading experience. The final deliverable combines a translated PDF, geometry- and digest-based paragraph alignment data, and an offline-capable side-by-side PDF viewer with bidirectional highlighting and scrolling.

The benchmark stresses translation meaning, scientific-token survival, page/render fidelity, text-native and OCR recovery, multi-column/rotated/multi-file reading order, calibrated alignment confidence, PDF-page overlay geometry, real browser interaction, local asset bundling, and keyboard/accessibility behavior.

## Builder Materials

Give the agent builder all four documents in `input/` together, in numeric order:

1. `01_task_goal.md`
2. `02_interface_and_delivery.md`
3. `03_requirements_and_constraints.md`
4. `04_resources.md`

They are the complete task contract. Do not provide the original benchmark, `test_cases/`, evaluator files, metadata, or construction work to the builder. The builder creates a submission containing `run_agent.py` and its support files, and configures only the dedicated Conda prefix named in the resource contract.

## Public Development Runs

The two public cases demonstrate the uniform Markdown input and core interaction patterns:

```bash
python run_agent.py \
  --input /absolute/path/to/scientific-pdf-translation-agent-v2/dev_cases/dev_001/input.md \
  --output /absolute/path/to/a-fresh-output/dev_001
```

Run `dev_002` the same way with a separate output directory. Resolve links relative to each `input.md`; do not copy or rewrite case assets. A development run is not complete until the builder has served the output root locally, opened `/viewer/index.html`, and tested both interaction directions with external network disabled.

## Hidden Test Isolation

Keep all six directories under `test_cases/` inaccessible during building and development. At evaluation time, expose only the active case directory and its request to the created agent. Use a fresh output directory and process/environment isolation for each case. Do not let one case read another case, earlier output, the evaluator, or benchmark metadata.

Enforce the declared 600-second, 4 GiB, filesystem, network, credential, and provider-call rules. Load provisioned variables without displaying them. Every supplied case is closed-corpus, so `serper` and `web_retrieval` counts must be zero.

## Final-Artifact Evaluation

For each hidden case, give the evaluator:

- The active `input.md` and referenced assets.
- The complete final output directory, including `translated.pdf`, `alignment.json`, `viewer/`, and `run_report.json` when present.
- Independent source/target PDF parse, extraction, page-geometry, render, and OCR evidence as useful.
- Browser screenshots, console/network capture, and interaction observations from a clean loopback-served session.
- `evaluator/rubric.md` and `evaluator/eval_prompt.md`.

Serve the output root, not the `viewer/` subdirectory, for example:

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory /absolute/path/to/case-output
```

Open `http://127.0.0.1:8765/viewer/index.html` with external network blocked. The reusable `evaluator/browser_probe.py` checks stable DOM semantics, nonblank canvases, bounded overlays, source click, target keyboard activation, reciprocal active state, off-screen scroll, resize, and unexpected requests. It is only a smoke helper; the evaluator must also perform the case-specific geometry, digest, translation, token, and hard-structure checks in `eval_prompt.md`.

## Scoring and Aggregation

Score every hidden case independently from 0 to 100 using the one global rubric. Report all six dimension scores and evidence before the total. The primary benchmark score is the unweighted arithmetic mean of the six hidden-case totals:

```text
overall = (test_001 + test_002 + test_003 + test_004 + test_005 + test_006) / 6
```

Keep one decimal place. Also retain per-case and per-dimension scores so a translation-strong but interaction-weak submission is not mistaken for a complete interactive reader. Do not use development-case scores in the hidden-test aggregate.
