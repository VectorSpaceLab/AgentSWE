# Evaluator Instructions

You evaluate one completed case at a time. You receive:

- The active case's `input.md` and referenced assets.
- The submitted `deck.pptx`, `source_manifest.json`, and `run_report.json` when present.
- The global rubric.
- Harness execution records, slide renders, or parse outputs when available.
- Isolated evaluator-side rendering, OOXML, spreadsheet/calculation, and public-web research tools.

Do not inspect the submission's source code, prompts, logs containing private reasoning, framework, or intermediate work. Do not compare implementation choices with a reference repository.

## Required Procedure

1. **Apply validity gates.** Confirm the required command launched, resource envelope was respected, `deck.pptx` exists, the package parses, slide relationships resolve, and at least one renderer can render every slide. Apply `0/100` only under the rubric's stated zero rules.
2. **Render every slide.** Inspect full-slide renders at a normal presentation viewport and, when necessary, zoomed crops. Check every slide for clipping, overlap, off-canvas objects, blank/broken media, missing glyphs, unreadable density, low contrast, and rasterized text.
3. **Inspect OOXML editability.** Unzip/parse the artifact and inventory visible text objects, `c:chart` parts and embedded data, DrawingML `a:tbl` tables, native shapes/connectors, notes-slide parts, images, accessible names/descriptions, object order, canvas size, and unresolved relationships. Compare chart caches/embedded values with rendered labels and source values.
4. **Audit sources and calculations.** Parse the manifest. For local-source cases, verify claims and calculations directly against assets. For live-web cases, independently inspect authoritative public sources or redundant evidence. Respect the case's cutoff and source priority. Recompute material formulas. Check that the manifest's access depth and unavailable leads are plausible; a listed URL is not proof that its body was read.
5. **Check every explicit case requirement.** Build a compact checklist from the active `input.md`, including slide count, required content, exact wording, language, canvas, layout, native-object, speaker-note, citation, source-boundary, and exclusion requirements. Do not use unpublished case-specific expectations.
6. **Score all six dimensions.** Use only the global rubric. For every deduction, cite concrete evidence by slide number and object/claim, manifest source/claim ID, report field, source location, render defect, or OOXML part.

For the historical Daily Papers case, independently reconstruct the dated ordering and distinguish historical placement from current cumulative engagement; do not rely on an evaluator-only oracle. For a blocked preferred source, evaluate the quality and honesty of recovery, not whether the preferred interface happened to work for the evaluator.

Reasonable differences in defensible thesis, visual style, source path, chart omission for non-comparable data, wording outside exact-copy constraints, or editable-diagram implementation must not lose points.

## Required Output

Return:

```text
Validity: PASS | ZERO - <reason>

Evidence Correctness and Analytical Integrity: <score>/25
Evidence: <specific evidence and deductions>

Case Compliance and Narrative: <score>/16
Evidence: <specific evidence and deductions>

Visual Storytelling and Analytical Representation: <score>/17
Evidence: <specific evidence and deductions>

OOXML Validity, Editability, and Technical Construction: <score>/18
Evidence: <specific evidence and deductions>

Provenance and Auditability: <score>/14
Evidence: <specific evidence and deductions>

Rendered Usability, Notes, and Accessibility: <score>/10
Evidence: <specific evidence and deductions>

Total: <integer>/100

Major errors:
- <error or "None">

Overall assessment: <concise evidence-based assessment>
```

Even when the score is zero, state the validity reason, major errors, and overall assessment. Do not award points for implementation elegance or effort.
