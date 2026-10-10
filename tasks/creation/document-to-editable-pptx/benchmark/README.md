# Evidence-to-Editable-Presentation Create-Agent Benchmark V2

## What This Measures

This benchmark measures whether a coding agent can create a reusable presentation agent that researches and qualifies evidence, performs auditable analysis, selects honest visual forms, and produces a reliable, accessible, natively editable PowerPoint briefing. The final deliverables are `deck.pptx`, `source_manifest.json`, and `run_report.json`.

The task is intentionally broader than document-to-slides conversion. Cases cover closed corpora and live research, source conflicts and recovery, historical evidence, calculations, scientific uncertainty, multilingual/RTL delivery, exact brand/layout rules, speaker notes, accessibility metadata, and native OOXML objects.

## Builder Inputs

Give the coding agent all four files under `input/` as its complete build specification:

- `01_task_goal.md`
- `02_interface_and_delivery.md`
- `03_requirements_and_constraints.md`
- `04_resources.md`

The builder creates its solution outside this benchmark directory and creates the dedicated Conda prefix specified in `04_resources.md`. Do not provide source-repository metadata, evaluator files, or hidden cases to the builder.

## Uniform Invocation

After activating the dedicated prefix, setting `PYTHONNOUSERSITE=1`, and loading the protected environment, every case uses:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

The harness must enforce the 600-second, 4 GiB, filesystem, network, credential, and model-call limits. It must keep `PYTHONNOUSERSITE=1` set for isolation. Each output directory must be fresh or contain only unrelated files that the agent must preserve.

## Public Development

Use exactly the two cases under `dev_cases/` while building:

- `dev_001`: closed-corpus fleet-pilot decision with calculations, a source conflict, brand asset, native chart/diagram, notes, and accessibility.
- `dev_002`: live-web NASA evidence briefing with full-source inspection, lawful visual handling, editable explanatory graphics, citations, and notes.

Run them independently. Example:

```bash
python run_agent.py \
  --input dev_cases/dev_001/input.md \
  --output /tmp/pptx-v2-dev-001
```

Assets resolve relative to each case's `input.md`. Inputs and assets are read-only.

## Hidden-Test Isolation

Keep `test_cases/` inaccessible to the builder and created agent before evaluation. At runtime expose only the active case directory, the submission, its dedicated prefix, normal system resources, and the protected credential file. Use a new output directory for each case. Do not expose evaluator prompts, other hidden cases, prior outputs, or construction metadata.

The six hidden cases are scored independently. They intentionally pressure different behaviors and must not be merged into one run.

## Evaluation

For one case, give the evaluator:

- That case's `input.md` and assets.
- The three final artifacts, when produced.
- Rendered slides and OOXML/parse results, or isolated tools to create them.
- Harness execution/resource records.
- `evaluator/rubric.md` and `evaluator/eval_prompt.md`.

The evaluator must render every slide, inspect OOXML editability, notes and alt text, audit citations and calculations against local sources or independent public research, and score only final artifacts and observable execution. Development cases are for feedback and are not included in the final benchmark score.

Final benchmark score = arithmetic mean of the six hidden-case integer scores, reported to one decimal place. A zero from one case remains in the mean; do not replace it with another case or discard it as an outlier. Keep the six dimension breakdowns for diagnosis.

## Operational Notes

Live-web evidence can drift. Preserve each run's manifests, timestamps, renders, and evaluator research notes. Historical ranking must be reconstructed from dated or redundant public evidence rather than a hidden answer file. The harness should use a stable PowerPoint-compatible renderer plus independent ZIP/XML inspection; a single successful render does not prove editability.
