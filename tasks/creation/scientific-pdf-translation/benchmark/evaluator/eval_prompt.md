# Evaluator Instructions

You are evaluating one case from the Interactive Scientific PDF Translation Benchmark. You receive the current case's `input.md` and assets, the created agent's final output directory, the global rubric, and any harness-generated parse/render/OCR/geometry/browser evidence. Evaluate final artifacts and observable viewer behavior only. Do not inspect the submission implementation, source code, prompts, intermediate files, model strategy, or similarity to another project.

## Procedure

1. Read the request and enumerate its observable language, mode, terminology, protected-token, page, layout, uncertainty, alignment, and interaction requirements.
2. Run the artifact validity gate. Parse `translated.pdf` with an independent PDF parser and render every page. Apply a zero rule only when the rubric explicitly requires it.
3. Inspect every source asset. Render every source page; use native extraction and OCR/visual inspection as appropriate. Compare source and target page count, displayed dimensions/rotation, reading order, scientific objects, and all page-local associations.
4. Build a page-by-page inventory of substantive source units. Inspect every heading, caption, note, footnote, and reference-title region and at least 12 substantive prose units distributed across the beginning, middle, and end when that many exist. Count material omissions and source-language leftovers explicitly. Quote short source/target phrases for translation deductions and accept multiple faithful renderings.
5. Search/extract every explicitly protected identifier, glossary target, and equation. Reconstruct every supplied table's row/column associations and every named figure, panel, diagram, callout, caption, and legend association from the target render, not extraction alone. Compare all result-bearing values when there are no more than 100; otherwise inspect a stratified sample of at least 30 plus every value named in the request. Extractability alone does not prove that a token is visible or attached to the right object.
6. Parse `alignment.json` strictly. Check schema version, document starts/counts, unique IDs/orders, status/confidence types/ranges, 64-character lowercase SHA-256 digests, one-based page references, finite bounded boxes, and source `document_id` values. Treat coordinates as normalized top-left after displayed rotation.
7. On each side, reconstruct visible text for representative anchors with coordinate-aware native extraction or OCR. Normalize with NFC, collapse whitespace, trim, hash UTF-8 text, and compare when extraction permits. Inspect at least six records spread across pages and types, plus every case-specific hard structure. Verify that boxes overlap the intended visible unit and target anchors contain translated rather than duplicated source text. For scan/OCR records, use visual/OCR evidence and judge confidence honestly when exact digest reproduction is not deterministic.
8. Serve the output root through an ordinary loopback static HTTP server. Block external network requests. Open `/viewer/index.html` in a clean local headless Chromium context at 1440 x 900, then repeat material layout checks at 1280 x 800. Capture console/page errors, failed local requests, unexpected external requests, and screenshots.
9. Verify both PDF panes have nonblank rendered page pixels and the required DOM hooks. Compare overlay bounding rectangles with the corresponding page rectangle and JSON normalized box. A detached transcript, decorative rectangle, or synchronized page number is not paragraph alignment.
10. Activate at least three representative common IDs from source to target and three from target to source. Include an off-screen counterpart so scroll movement is testable, and include the current case's hard structure: native/scan/landscape for `dev_002`; rotation/dense tokens for `test_001`; split paragraph for `test_002`; low-confidence scan for `test_003`; both source documents for `test_004`; RTL prose/footnote for `test_005`; dense callout/caption for `test_006`. Check click, Enter, and Space across the sample.
11. For each activation, verify all overlays with the selected ID receive `is-active` and `aria-current="true"` on both sides, the prior ID clears, `[data-alignment-confidence]` updates, and the first opposite anchor becomes substantially visible inside its pane. For unresolved records, verify the viewer announces uncertainty and does not claim false navigation.
12. Score the six dimensions independently. Then evaluate every Core-Quality Ceiling in the rubric and apply the lowest triggered ceiling to the summed score. Do not deduct twice for one fact unless it causes distinct harm in different dimensions; explain each independent effect. Cite concrete file/page/paragraph/box/DOM/browser evidence for every deduction, full-credit claim, and ceiling decision. The dimension maxima must sum to exactly 100 before any ceiling.

`browser_probe.py` is a reusable smoke helper for the stable DOM contract. Its pass result is not sufficient for full interaction credit; use case-aware geometry and visual checks as described above.

## Required Evaluation Output

```markdown
# Evaluation: <case id>

## Validity
- Status: valid | execution failure
- Evidence: <launch/time/memory/PDF parse and render facts>

## Dimension Scores
| Dimension | Score | Maximum | Evidence |
| --- | ---: | ---: | --- |
| Semantic translation completeness and terminology | ... | 35 | ... |
| Scientific object and data fidelity | ... | 35 | ... |
| Page, render, and reading structure | ... | 12 | ... |
| Alignment auditability | ... | 8 | ... |
| Bidirectional offline viewer | ... | 8 | ... |
| Delivery integrity and accessibility | ... | 2 | ... |

## Total
- Raw dimension sum: <score>/100
- Applicable core-quality ceiling: <none, or ceiling and cited trigger>
- Final score: <score after ceiling>/100

## Browser Checks
- Local/offline loading: ...
- Source-to-target actions: ...
- Target-to-source actions: ...
- Hard-structure action: ...
- Console/network/accessibility evidence: ...

## Major Errors
- <only material defects, or `None observed`>

## Overall Assessment
<two to four evidence-based sentences>
```
