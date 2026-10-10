# Strict Final-Artifact Rubric (100 points)

This rubric intentionally gives **70/100 points to the core translation result**: semantic translation and the fidelity of scientific objects. A polished viewer cannot compensate for wrong, missing, or structurally unusable scientific content.

## Validity and Execution Rules

Apply these rules before dimension scoring:

- The agent cannot launch through the required command: **0/100**.
- The run exceeds 600 seconds or 4 GiB: **0/100**.
- No `translated.pdf` is produced: **0/100**.
- `translated.pdf` is corrupt, encrypted without supplied credentials, has no renderable page, or cannot be parsed by an ordinary independent PDF parser: **0/100**.
- The run reports success while the output contains only placeholders or an unrelated document: **0/100**.
- A parseable, case-related translation with missing or broken alignment/viewer artifacts receives ordinary dimension scores and any applicable quality ceiling below; it is not automatically an execution failure.
- A missing `run_report.json` or another nonessential diagnostic file does not by itself force zero.
- A valid but poor artifact receives an ordinary score. Do not convert quality defects into an execution failure.

Inspect only the current case request/assets and final output bundle. Harness-side parsers, renderers, OCR, hash tools, a loopback static server, and a local headless browser may be used. Do not inspect submission source code, prompts, logs, intermediate work, architecture, or model/tool choices.

## Core-Quality Ceilings

Score all six dimensions first, then apply the **lowest applicable ceiling**. A ceiling limits the final total; it does not create an execution failure. Cite the exact page, source unit, and target evidence that triggers it. Do not apply a ceiling for a cosmetic defect or a defensible translation/layout variation.

| Observable core failure | Maximum total |
| --- | ---: |
| Wrong target language/mode for a substantive region, or at least 25% of substantive source units untranslated, omitted, or replaced by source-language text | 35 |
| A meaning reversal or invented claim in a safety-, conclusion-, limitation-, result-, or instruction-bearing statement | 45 |
| Two or more material formula/value/sign/unit/identifier corruptions, or one corruption that changes the main scientific conclusion | 40 |
| A required table, figure, equation group, or other named dense scientific region is materially unusable because values/labels/panels/rows/columns lost their associations | 55 |
| Two or more required dense-region types or pages are materially unusable, or an entire substantive page has wrong orientation/reading order | 35 |
| More than 10% but less than 25% of substantive units are untranslated or omitted | 60 |
| `alignment.json` and the viewer are both missing or nonfunctional although the translated PDF is valid | 75 |

When multiple bullets describe the same underlying defect, apply only the lowest ceiling, not an additional numerical deduction. The dimension scores should still reflect each distinct observable consequence, such as corrupted content versus unreadable layout.

## 1. Semantic Translation Completeness and Terminology - 35 points

**Observable object:** All visible and extractable target-language prose in `translated.pdf`, compared with every in-scope source heading, paragraph, caption, note, table/figure label, footnote, reference title, and authoritative terminology asset.

**Required evaluator coverage:** Inventory substantive source units page by page. Inspect every heading/caption/note/reference-title region and at least 12 substantive prose units distributed across the beginning, middle, and end when that many exist. Explicitly count material omissions and source-language leftovers. A single strong first page is not evidence of document-wide fidelity.

**Full credit (32-35):** At least 98% of substantive units are translated. No substantive source-language paragraph remains unless the request requires bilingual output. Meaning, negation, modality, causal limits, uncertainty, cross-references, and argument structure are preserved. All authoritative glossary targets are exact and consistent, deprecated terms are absent, and the language is fluent and appropriate for the named audience.

**Middle (18-31):** Between 90% and 98% of substantive units are translated and the main argument remains correct, but there are isolated omissions, awkward passages, qualification drift, reference-title omissions, or inconsistent noncritical terminology. Award at most 24 if a result, limitation, conclusion, or required terminology rule is materially weakened even without a full reversal.

**Low (0-17):** Less than 90% coverage; partial translation or summarization; wrong language/mode in a substantive region; repeated source-language leftovers; prohibited/deprecated terminology; invented scientific prose; or a material reversal of meaning, negation, modality, causality, or uncertainty.

**Typical severe errors:** Dropping a `does not` limitation; converting association into causation; translating native-text pages but omitting scans; leaving captions, references, or table prose untranslated; aligning a bilingual source copy instead of the translation; using inconsistent authoritative terminology across documents.

**Do not penalize:** Faithful alternate wording, regional phrasing consistent with the requested target, clause reordering required by target grammar, or reasonable local line breaking.

## 2. Scientific Object and Data Fidelity - 35 points

**Observable object:** Formulas, variables, identifiers, citations, names, values, signs, decimal precision/convention, units, chemical/species notation, protected strings, table cells and header associations, figure/panel labels, diagram edges, scale bars, footnotes, and dense scientific regions in target extraction and page renders.

**Required evaluator coverage:** Check every explicitly protected token and every equation. Reconstruct every supplied table's row/column associations and every named figure/panel/callout association from the rendered target, not text extraction alone. Compare all result-bearing numeric values when the case contains no more than 100; otherwise use a stratified sample of at least 30 plus every value named by the request.

**Full credit (32-35):** All protected strings and material data are exact and visibly findable. Equations are mathematically equivalent. Values, signs, precision, units, identifiers, citations, rows, columns, panels, legends, arrows, captions, and footnotes retain their source associations. Tokens inside landscape, scanned, dense, CJK, and RTL regions remain legible and attached to the correct object.

**Middle (15-31):** No conclusion-changing corruption, but there are a few isolated secondary-label errors, low-impact formatting differences, or localized association weaknesses. Award at most 24 for any material table/figure association error; at most 18 when one required dense region is materially unusable; and at most 12 for repeated formula/value/unit corruption even when the overall PDF remains parseable.

**Low (0-14):** A material formula, value, sign, unit, identifier, or citation changes; table rows/columns flatten so facts cannot be reliably associated; figure/panel labels attach to the wrong object; dense-region labels disappear; RTL ordering corrupts Latin identifiers; or scientific visuals are replaced or obscured.

**Typical severe errors:** `-0,24` becomes `+0.24`; an exponent disappears; `Na+/H+` becomes ordinary prose; `ROI_A3` moves to the wrong panel; a five-column table renders all values in its first column; reference markers detach from their claims.

**Do not penalize:** Visually equivalent mathematical glyphs, font substitution, nonsemantic operator spacing, or translated ordinary-language labels when tokens and associations remain correct.

## 3. Page, Render, and Reading Structure - 12 points

**Observable object:** Every rendered source/target page; page count/order; displayed dimensions and rotation; reading order; glyph coverage; text direction; and relationships among prose, columns, tables, figures, captions, footnotes, and references.

**Full credit (11-12):** Required sequence, dimensions, and rotations are preserved. Every page renders without clipping, overlap, failure blanks, missing-glyph boxes, or critical occlusion. Columns read correctly; tables/figures remain interpretable; captions/notes/references stay associated; CJK and Arabic/Latin text render correctly; searchable target text remains available where reasonable.

**Middle (6-10):** Usable overall with isolated crowding, small type, modest reflow, limited unnecessary rasterization, or a few extraction-order defects. Award at most 7 if one material dense region loses reading structure and at most 5 if a substantive page is sideways/upside down.

**Low (0-5):** Wrong/missing pages, broken rotation, scrambled columns, widespread clipping/overlap, unreadable tables, disconnected RTL text, tofu/mojibake, unjustified flattening, detached captions, or replacement/occlusion of scientific visuals.

**Do not penalize:** Restrained visual redesign, different readable fonts, modest local reflow, or raster preservation of source scans and scientific imagery.

## 4. Alignment Auditability - 8 points

**Observable object:** `alignment.json`, independently parsed PDFs/renders/OCR, reconstructed normalized text digests, and geometric correspondence between declared anchors and visible PDF content.

**Full credit (8):** The declared schema is valid; document/page mappings, IDs, positive reading orders, rotated top-left normalized boxes, and SHA-256 digests are correct. Multi-fragment units share one semantic ID. Boxes tightly cover stated visible units; coverage includes all major content classes; reading order follows source structure; confidence/status is calibrated and uncertain OCR/layout links are disclosed.

**Middle (4-7):** Machine-usable and broadly correct, but coverage is incomplete, boxes are loose, some digests cannot be reproduced, one rotation/split is mishandled, reading order has local errors, or confidence is weakly calibrated. Award at most 5 when fewer than 90% of sampled anchors point to their declared visible text.

**Low (0-3):** Missing/malformed JSON; invalid pages/boxes; decorative or detached geometry; systematic source/target mismatch; page-level boxes standing in for semantic units; false high confidence; placeholder digests; or lost split/multi-document identity.

**Do not penalize:** Defensible paragraph segmentation, coherent compact-cell grouping, minor box padding, or explicit unresolved records where evidence is genuinely insufficient.

## 5. Bidirectional Offline Viewer - 8 points

**Observable object:** `/viewer/index.html` served from the output directory over loopback HTTP with external network blocked, using the required DOM hooks and real rendered PDF page surfaces.

**Full credit (8):** Both PDFs load side by side. Geometry-derived overlays scale with pages. Click, Enter, and Space activation works in both directions; all same-ID fragments receive active/ARIA state; prior state clears; an off-screen counterpart scrolls substantially into view; hard structures work; highlighting is visible without obscuring text; uncertainty is announced rather than faked.

**Middle (4-7):** Ordinary bidirectional prose links work, but one keyboard path, direction, off-screen scroll, resize behavior, multi-fragment unit, or case-specific hard structure is incomplete. Award at most 5 if a hard-structure interaction navigates to a declared box that does not contain the intended text.

**Low (0-3):** Viewer fails; uses screenshots/detached transcript instead of PDF overlays; is one-directional or page-number-only; depends on remote assets; overlays do not correspond to boxes; active state is invisible; or scroll targets are wrong.

**Do not penalize:** Renderer choice, toolbar styling, smooth versus immediate scroll, reasonable zoom defaults, or exact highlight color when behavior and contrast remain clear.

## 6. Delivery Integrity and Accessibility - 2 points

**Observable object:** Required inventory, offline asset graph, both required viewports, browser console/network results, focus/labels/status semantics, `run_report.json`, overwrite behavior, and accidental/sensitive content.

**Full credit (2):** Required files resolve locally; viewer copies are correct; no external runtime request occurs; both viewports are usable; controls have labels and visible focus; report paths/counts/provider calls match artifacts; no secret, traceback, placeholder, stale output, or unrelated path leaks.

**Middle (1):** The bundle is usable but has one isolated semantic, focus, responsive-overflow, harmless-console, report-count, or locally repairable path defect.

**Low (0):** Remote runtime dependency, broken serving paths, wrong source/target copies, inaccessible hit regions, materially false report, stale artifacts, secrets, debug traces, or placeholders.

**Do not penalize:** Missing optional diagnostics, absence of a service worker, English viewer chrome, or lack of direct `file://` support.
