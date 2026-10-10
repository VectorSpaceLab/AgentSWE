# Global Final-Artifact Rubric (100 Points)

## Execution and Validity Rules

- If the agent cannot launch through the required command, assign **0/100**.
- If the run exceeds 600 seconds or 4 GiB, or the harness establishes prohibited resource use, assign **0/100**.
- If no `deck.pptx` is produced, assign **0/100**.
- If `deck.pptx` is corrupt, cannot be parsed as OOXML, has no slides, or cannot be rendered by the evaluation renderer after one normal renderer retry, assign **0/100**.
- A valid but weak, inaccurate, visually poor, or minimally editable deck receives an ordinary rubric score; do not convert quality defects into an automatic zero.
- Missing or malformed `source_manifest.json` or `run_report.json` is severe requirement failure but does not by itself force zero when the deck is valid. Score the observable deficiencies below.
- A missing optional diagnostic file never forces zero.
- Score only the submitted artifacts, their rendered/parsed behavior, the case input/assets, and observable harness execution records. Do not inspect or score source code, prompts, framework, agent loop, intermediate files, or similarity to any reference implementation.

Before scoring, render every slide, parse `source_manifest.json` and `run_report.json`, and inspect the OOXML package. Verify material claims and calculations against supplied sources or evaluator research. Do not accept a manifest entry as proof merely because it exists.

## 1. Evidence Correctness and Analytical Integrity - 25 Points

**Observable object:** factual text, values, labels, comparisons, translations, uncertainty statements, formulas, chart/table data, and visual representations in the deck, checked against case evidence and manifest calculations.

**22-25 (full):** Material claims are supported and accurately bounded. Required calculations reproduce from cited inputs with correct denominators, periods, sign, units, precision, and aggregation. Conflicts follow the case's priority rules or are shown explicitly. Historical cutoff, publication date, sample size, and uncertainty are preserved. Visuals do not imply unsupported causality or comparability.

**11-21 (middle):** The main thesis is evidence-based, but there are several minor errors or one material weakness: a rounding/label problem, incomplete caveat, partially reproducible formula, weak conflict handling, or a chart whose comparison needs qualification. No pervasive fabrication.

**1-10 (low):** Multiple unsupported or wrong claims, major arithmetic or denominator errors, mistranslation, inappropriate source precedence, omitted uncertainty, present-day data substituted for a historical cutoff, or misleading quantitative encoding materially damages the briefing.

**0:** The deck is predominantly fabricated or contradicts the authoritative case evidence.

**Typical severe errors:** inventing a ranking or vote count; citing a search snippet as a read paper; mixing fiscal and calendar periods; averaging rates that must be recomputed from counts; using gross capture as net durable removal; stating preliminary investigation material as probable cause; inventing missing screenshots or translations.

**Do not penalize:** a different supportable thesis, calculation order, rounding convention allowed by the case, or defensible choice not to chart incompatible data.

## 2. Case Compliance and Narrative - 16 Points

**Observable object:** slide count/canvas, required sections, sequence, explicit inclusions/exclusions, audience fit, source boundary, decision or learning objective, and the logical progression of the rendered deck.

**14-16 (full):** All material case requirements are present in the requested form. The opening frames the actual decision/question, slides build a coherent evidence-led argument, each slide has a clear job, and the close enables the requested decision, action, or learning outcome. Explicit brand/layout/language/exclusion rules are honored.

**7-13 (middle):** Most required content is present and usable, but one material requirement or several smaller constraints are missing, weakly placed, or generic. The narrative is understandable but has redundant, abrupt, or agenda-like sections.

**1-6 (low):** Major required sections are absent, the deck uses the wrong audience/format/language, ignores the evidence boundary, or behaves like a document dump rather than a briefing.

**0:** The valid deck addresses a different task.

**Typical severe errors:** generic agenda replacing required impact opening; omitted board resolution; hidden source conflict; absent top-three ranking; missing one required language; violating a closed-corpus rule as shown by artifacts/run records.

**Do not penalize:** reasonable slide combinations, alternate section labels, or a sequence that differs while preserving every communication job and requested slide count/range.

## 3. Visual Storytelling and Analytical Representation - 17 Points

**Observable object:** rendered composition and use of charts, tables, diagrams, imagery, annotations, hierarchy, and visual pacing to communicate evidence.

**15-17 (full):** The deck uses substantive visuals to carry the argument, with a coherent but varied visual system. Chart types fit the analytical question; tables expose necessary raw values; diagrams clarify mechanisms or sequences; imagery is relevant and attributed. Takeaway titles, annotations, scale, labels, legends, and ordering make the evidence quickly understandable. Visual density fits presentation use.

**8-14 (middle):** Several useful visuals communicate correctly, but some slides fall back to dense bullets, repetitive panels, weak hierarchy, a suboptimal chart choice, or underdeveloped annotation. The deck is still presentable and interpretable.

**1-7 (low):** Decoration dominates evidence, most slides are title-plus-bullets, quantitative material is pasted as unreadable tables, graphics are generic, or important visual encodings are confusing or misleading.

**0:** The rendered deck provides no meaningful visual communication beyond plain text despite the case requirements.

**Typical severe errors:** decorative AI/space/disaster imagery replacing evidence; plotting incomparable scientific ranges together; dual axes that create a false relationship; tiny long tables; reused paper figures without a clear explanatory purpose.

**Do not penalize:** restrained aesthetics, faithful shape-based explanatory graphics instead of uncertain third-party media, or omission of an optional chart when a native table is analytically more honest.

## 4. OOXML Validity, Editability, and Technical Construction - 18 Points

**Observable object:** `deck.pptx` package structure and relationships; native text, chart, embedded workbook/cache, table, shape, connector, notes, and media objects; canvas and render stability.

**16-18 (full):** The package is structurally sound. Visible text remains editable text. Required quantitative charts have native `c:chart` parts with editable data and values consistent with the render. Required tables use DrawingML table objects. Timelines/processes/causal or mechanism diagrams use editable shapes/connectors. Requested notes exist in actual notes-slide parts. Media relationships resolve. No slide-sized raster substitute or rasterized text masquerades as editable content.

**9-15 (middle):** The deck is broadly editable and valid, but one required object class is shape-simulated when a native object was required, some chart data or relationships are incomplete, grouping impairs editing, notes are partially missing, or several data-bearing elements are rasterized. Core text and most structures remain editable.

**1-8 (low):** Most visual content is flattened, charts/tables are images, required notes are absent, embedded data are missing/inconsistent, or widespread OOXML relationship/font/media problems make normal editing unreliable despite successful rendering.

**0:** The deck barely passes parsing but is effectively a set of slide images or has pervasive package damage.

**Typical severe errors:** one full-slide PNG per slide; screenshots of CSV tables; invisible text placed over rasterized content; native chart labels that disagree with embedded values; notes typed into visible slide text instead of notes parts.

**Do not penalize:** ordinary raster photographs, supplied marks, maps, paper figures with valid use basis, or explanatory diagrams made from native shapes instead of SmartArt.

## 5. Provenance and Auditability - 14 Points

**Observable object:** on-slide citations, image attributions, `source_manifest.json`, access-depth reporting, source/claim/slide mapping, calculation records, and recovery disclosure.

**12-14 (full):** Every material factual slide and visual has a readable source marker. The valid manifest resolves IDs to exact local paths or public URLs, publisher/title/date/access time, access status/depth, use basis, cited slides, and material claim mappings. Calculation records reproduce displayed results. Blocked/partial sources and redundant evidence are disclosed honestly. Historical ranking/engagement or source-priority decisions are traceable without a hidden oracle.

**6-11 (middle):** Most evidence is traceable, but some claims or visuals lack mapping, citations are incomplete, access depth is vague, calculation records omit an input/rounding detail, or unavailable leads are not fully documented. An evaluator can still reconstruct the main argument.

**1-5 (low):** Citations are generic homepages or unreadable URLs, the manifest is sparse/inaccurate, claims map to sources that do not support them, search snippets are reported as full pages, or calculations cannot be reconstructed.

**0:** No usable provenance exists or the manifest is materially fabricated.

**Typical severe errors:** citing an inaccessible lead as evidence; citing an abstract for a table-only numerical claim; failing to identify a reused visual; recording zero calls despite observable retrieval; omitting source priority/conflict resolution from the audit trail.

**Do not penalize:** a different source-ID scheme, additional manifest fields, or reputable redundant primary evidence that differs from the evaluator's path but supports the same claim.

## 6. Rendered Usability, Notes, and Accessibility - 10 Points

**Observable object:** every rendered slide at normal presentation size, requested speaker notes, OOXML accessible names/descriptions, reading order, language rendering, contrast, and practical handoff quality.

**9-10 (full):** No clipping, unintended overlap, off-canvas content, blank media, broken glyphs, or unreadable density appears on any slide. Type and citations meet case minima; contrast and spacing support the audience. Requested notes add useful delivery guidance. Meaningful non-decorative visuals have accurate accessible names/alt text, object order is logical, and material distinctions do not depend on color alone. Multilingual/RTL text renders correctly when required.

**5-8 (middle):** The deck is usable but has a few localized problems such as small source text, crowded labels, weak contrast, one missing alt description, imperfect object order, thin notes, or a minor glyph/RTL issue that does not destroy meaning.

**1-4 (low):** Recurrent overlap/clipping, missing glyphs, illegible density, absent requested notes, pervasive missing alt text, color-only encoding, or broken RTL makes presentation or reuse difficult.

**0:** Rendering is technically possible but the slides are functionally unreadable.

**Typical severe errors:** source lines outside the canvas; labels obscuring chart data; Arabic characters reversed or disconnected; alt text that says only `image`; notes duplicated from slide text; a missing optional image leaving a blank placeholder.

**Do not penalize:** small renderer-specific font reflow that does not clip or alter meaning, concise notes that fully meet the requested purpose, or decorative objects omitted from the accessibility tree.

## Total

The six dimensions total exactly **100 points**. Assign integer scores only and cite slide-, manifest-, report-, source-, render-, or OOXML-level evidence for every deduction.
