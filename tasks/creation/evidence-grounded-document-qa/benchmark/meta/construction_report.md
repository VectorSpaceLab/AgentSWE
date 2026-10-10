# Construction and Independent Audit Report

## Parameters and assumptions

- Create-Agent case; slug `evidence-grounded-document-qa-agent-hard-v4`.
- Exactly four builder input documents, two public development cases, and six hidden test cases.
- Uniform UTF-8 Markdown request and `python run_agent.py --input <input.md> --output <output_dir>` interface.
- Closed corpus for retrieval: no search, scrape, browser-network, followed-link, or other external retrieval. The separately declared GATEWAY model API is allowed as counted inference.
- Per-case envelope: 600 seconds, 4 GiB RAM, 300 total model attempts, and 100 image-bearing model attempts.
- Final-result scoring is final-artifact-only and totals 100. The independent implementation rubric uses the exact shared eight IDs and weights totaling 100.
- Harbor, v3-r2, and all sibling benchmarks are outside the edit boundary. Runtime provisioning claims outside v4 were not inspected.

## Coverage design

The two public cases calibrate HTML/CSV/SVG calculation and PDF/DOCX/XLSX correction workflows. The six hidden cases are structurally different rather than entity substitutions: dual quantitative/visual inspection; temporal fare and transfer policy; assay revision plus invalid-well calculation; average-end-area sediment estimation; distractor-heavy multi-revision procurement synthesis; and corrected/aligned sensor-event reconstruction.

Every hidden case combines at least three modalities, a multi-hop calculation or temporal/state transition, explicit entity/revision/distractor control, and a justified abstention boundary. Hidden modality signatures are unique. No runtime Markdown asset supplies a shortcut transcript of a PDF, SVG, table, or raster. Visual cases keep quantitative inputs in native tables/CSV and require direct figure or raster evidence for the visual sub-question.

## Independent audit findings and repairs

Builder/construction claims were treated as untrusted and checked against the files and executable validators.

1. `evaluator/code_rubric.md` described the shared output shape but had no parseable example object. It now contains exactly one JSON example with the exact top-level contract, the eight shared IDs and fixed maxima, `code_raw_score`, `code_applied_caps`, `code_score`, cap-item shape, arithmetic rules, source-unavailable rule, and result/code-axis separation. `validate_benchmark.py` now parses and enforces that example.
2. Valid-fixture mutation found false acceptance of empty success artifact paths, a non-array success `errors` value, and a `supported` claim backed only by contradicting evidence. `validate_artifacts.py` now enforces exact successful artifact inventory, report field/type/counter consistency, optional resource-stat types, and minimum status/relation coherence.
3. Native-locator mutations showed that generic HTML byte ranges, whole-page and grazing PDF rectangles, grazing SVG rectangles, and near-full-image raster rectangles were accepted. `source_native.py` now enforces source-type-specific locator kinds, PDF coverage/breadth thresholds, SVG coverage/breadth thresholds, tighter raster breadth, rectangular XLSX ranges, uppercase A1 references, and PNG CRC/scanline integrity.
4. Viewer mutations showed that relative file dependencies passed static checks and the browser harness exercised only one claim/evidence pair without complete reciprocal sets, relation/status state, exact-byte target proof, post-interaction request checks, or visible-element geometry. Static validation now rejects relative/file/remote element and CSS dependencies, checks source-target metadata, and rejects globally reused manifest DOM IDs. The browser harness now exercises every manifest claim and evidence target, requires visible nonzero reciprocal targets, validates complete ID sets and state, hashes each source target's exact-byte data/Blob control, checks claim+evidence fragments, observes all requests after interaction, records runtime exceptions, and cleans browser profiles/processes on every exit path.
5. Hidden case 006 originally showed a green H-8 light while the packet said both panel lights still used uncorrected humidity, making the photograph irreconcilable. The signed handoff now records device-specific panel basis: H-7 remained unrebooted on uncorrected values, while H-8 had rebooted onto corrected values. The mixed photograph is therefore source-supported rather than dependent on an invented exception.
6. Four case requests used “zero network/external calls,” which conflicted with the declared remote model API. They now state the intended zero-retrieval policy and separately permit/count the declared model API.
7. The unnecessary root `REPAIR_NOTES.md` was removed; durable audit history is consolidated here.

## Source-native and review contract

Stable claim/evidence IDs link exactly between claims JSON and the review manifest. Supported native locators cover exact UTF-8 byte ranges, HTML elements plus exact outer byte ranges, CSV fields plus exact field byte ranges, XLSX cells/rectangular ranges, positioned PDF rectangles, DOCX body paragraphs/table cells, SVG IDs/geometry, and normalized raster regions. Exact source bytes and SHA-256 values are bundled once per cited source.

Every source-target element must expose manifest-matching source ID, hash, and native-locator metadata plus a visible exact-byte data/Blob control. The offline API state includes selected and complete reciprocal IDs, status, confidence, relation, source ID/hash, native locator, and URL fragment. Static validation is complemented by a required real-browser interaction pass.

## Provenance, leakage, and separation

All 33 runtime assets are synthetic and generated locally by `meta/generate_assets.py`. `meta/asset_provenance.json` records byte size, SHA-256, synthetic provenance, and CC0-1.0 fixture status for each asset. No personal, private, or copied copyrighted data is present.

Each case contains only `input.md` and referenced runtime assets. No case contains an answer key, expected output, claim manifest, case rubric, forbidden-error list, or judge context. Public and hidden assets have zero identical hashes and zero exact numeric-source-fragment overlap under the benchmark check. Public builder inputs and development cases contain none of the hidden entity identifiers sampled during audit. Evaluator regression fixtures use the public `dev_001` source only and contain no hidden answer facts.

## Resource feasibility

The 33 runtime assets total 62,758 bytes. Native files use bounded deterministic structures: small multi-page positioned-text PDFs, compact Office ZIP/XML files, modest CSVs, simple SVGs, and 640–820-pixel-wide RGB PNGs. Exact-byte base64 expansion remains small. Independent reconstruction confirmed that every requested calculation has finite inputs, coherent units, deterministic revision priority, and a reachable answer or explicit abstention. These packets are feasible within 600 seconds and 4 GiB for a competent local parser plus a bounded number of model/image requests.

The external runner/model provisioning itself was not independently verified because the audit was restricted to v4 and did not inspect Harbor or sibling infrastructure. `input/04_resources.md` therefore remains the declared runtime contract rather than a claim proven from an out-of-scope runner.

## Validation record — August 24, 2026

- Regenerated all assets twice with `PYTHONDONTWRITEBYTECODE=1 python3 meta/generate_assets.py`. Both runs produced 33 assets, and the sorted path+byte aggregate was `b9ddca0358539e68a5a82df97d0ca6e346a7f6c7822905a645813d9bedb95ab3` both times.
- Independently parsed every runtime asset without importing the benchmark validator: 5 PDF, 6 DOCX, 3 XLSX, 6 HTML, 6 CSV, 4 SVG, and 3 PNG files. ZIP/XML structures, positioned PDF text, element IDs/geometry, CSV rows, PNG dimensions/colors, and MIME signatures were sampled. All three raster assets were visually inspected.
- Independently recomputed the requested efficiencies, corrected means/maxima, weighted mean, fare/refund timings, assay concentrations, reach volumes/mass, project energy/cost, and aligned alarm durations. The source packets were internally coherent after the case-006 repair.
- Python AST parsing over every Python file passed. `node --check evaluator/validate_viewer_interaction.mjs` passed.
- `PYTHONDONTWRITEBYTECODE=1 python3 evaluator/validate_benchmark.py .` passed with four builder inputs, two development cases, six hidden cases, 33 runtime assets, zero duplicate asset hashes, zero public-hidden hash overlap, zero repeated numeric source fragments, six unique hidden modality signatures, a 100-point final rubric, and the exact shared code-rubric JSON contract.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evaluator/tests -p 'test_*.py' -v` passed 15 tests. Positive coverage includes every runtime asset/modality, exact UTF-8 byte ranges, rectangular XLSX ranges, and the complete dev contract fixture. Negative coverage includes bad bundle bytes, manifest drift/DOM reuse, wrong HTML/CSV ranges, generic HTML byte locators, invalid XLSX/DOCX targets, non-overlapping/broad/grazing PDF/SVG/raster regions, corrupt PNG CRC, status/relation mismatch, malformed success reports, relative viewer dependencies, and a missing shared code-contract example.
- The generated dev-only contract fixture passed both `validate_artifacts.py` and `validate_source_native_review.py` with exact bytes, source metadata, manifest linkage, and source-native HTML targeting.
- `node evaluator/validate_viewer_interaction.mjs ...` returned its documented skip status because no Chromium-family executable exists in this environment. No browser package was installed and no npm cache, log, package tree, profile, or browser output was retained.
- The v3-r2 preservation aggregate recomputed as `05d8cf57204aca4744510fd834d8e49b3188e3baef663f4099d112a8fa89b15e`, exactly matching `meta/source_copy_baseline.json`.

## Known limitations

- Actual browser interaction remains unproven in this environment. The all-target harness is syntax-valid and its static prerequisites/fixture structure pass, but a release evaluator must run it in a compatible offline Chromium-family browser or perform and record the equivalent complete manual matrix.
- The dependency-free PDF parser intentionally supports the deterministic positioned-text PDF subset used by these fixtures; semantic visual review remains necessary for arbitrary candidate interpretations.
- Raster validation proves PNG integrity, bounds, nontrivial visual variation, and target breadth, but it cannot generically prove the semantic label of a colored object. Evaluators must visually inspect material raster observations.
- External GATEWAY availability, credentials, and runner enforcement were not testable within the v4-only boundary.

## Final inventory

The release contains 63 files: 4 builder inputs; 8 case requests; 33 runtime assets; README; final-result and shared implementation rubrics; evaluator prompt; source-native helper; benchmark, artifact, review, and browser validators; 3 evaluator test/fixture files; deterministic asset generator; provenance and source-copy metadata; source analysis; reference baseline; and this report. No answer oracle or per-case evaluator file is present.
