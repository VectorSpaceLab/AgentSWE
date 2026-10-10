# Evidence-Grounded Document QA Agent — hard v4

This Create-Agent benchmark measures whether a builder can implement a closed-corpus document QA agent that produces correct multi-hop answers and a genuinely source-native, offline evidence-review package. V4 replaces the short and partly duplicated v3 fixtures with substantive synthetic corpora whose facts are distributed across PDF, DOCX, HTML, CSV/XLSX, SVG, and raster sources. Cases require revision priority, entity separation, calculations, table/figure reasoning, distractor resistance, contradiction representation, and claim-level abstention.

## Builder handoff

Give the builder exactly the four documents under `input/`, together with the two public directories under `dev_cases/`. Do not expose `test_cases/`, evaluator files, or metadata during implementation. The builder creates a general agent implementing the uniform command:

```bash
python run_agent.py --input <case/input.md> --output <output_dir>
```

Each case's `input.md` and `assets/` directory are its complete runtime input. Cases contain no answer key, oracle manifest, case rubric, or judge-only context.

## Public development runs

Run both public cases independently into fresh output directories. A successful run produces `answer.md`, `claims_and_citations.json`, `review.html`, `review_manifest.json`, `source_bundle.json`, and `run_report.json`. Use the evaluator validators to catch contract, locator, byte/hash, and viewer-linkage errors; development answers are not a substitute for hidden-case generalization.

## Hidden evaluation

Keep all six `test_cases/` isolated until evaluation. For each hidden case, invoke the same command and provide the evaluator with only that case input/assets, the final output artifacts, parser/render results, `evaluator/rubric.md`, and `evaluator/eval_prompt.md`. Run:

```bash
python evaluator/validate_artifacts.py <case_dir> <output_dir>
python evaluator/validate_source_native_review.py <case_dir> <output_dir>
```

When a compatible Chromium-family executable is available, also run:

```bash
node evaluator/validate_viewer_interaction.mjs <output_dir>/review.html <output_dir>/review_manifest.json
```

The browser harness exercises every claim and evidence target, hashes each target's exact-byte source control, and fails on external or secondary file requests. If no compatible browser exists, the evaluator must perform the same complete API matrix manually in an offline browser and record that evidence; static presence of API names or one working pair is not proof of interaction correctness.

Score each hidden run independently with the 100-point final-artifact rubric. Report the arithmetic mean, median, minimum, and all six per-case scores. Keep the separate 100-point implementation score from `evaluator/code_rubric.md` as an independent axis; never average code and result scores.

## Reproducibility and provenance

All runtime documents and figures are synthetic, contain no personal data, and are generated deterministically by `meta/generate_assets.py`. `meta/asset_provenance.json` records hashes, sizes, and CC0-1.0 fixture provenance. Cases are closed-corpus and require zero web retrieval. The intended per-case envelope is 600 seconds, 4 GiB RAM, at most 300 combined model requests, and at most 100 image-bearing requests.
