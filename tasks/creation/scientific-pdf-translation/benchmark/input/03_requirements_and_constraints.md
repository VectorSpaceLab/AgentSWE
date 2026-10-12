# Requirements and Constraints

## Functional Requirements

### 1. Interpret the Complete Request

Extract the ordered sources, languages, monolingual or bilingual mode, terminology authority, protected strings, document-specific instructions, evidence boundary, and viewer preferences. Explicit case requirements are authoritative.

### 2. Inspect Every Page and Recover Reading Order

Parse and render every page. Distinguish translatable prose from formulas, variables, code, identifiers, citations, URLs, page furniture, tables, figures, captions, footnotes, references, and protected marks. Recover reading order across multi-column, landscape, rotated, scan-only, and mixed native/scanned pages. Use OCR or allowed multimodal analysis when extraction is insufficient.

### 3. Translate Faithfully

Translate all in-scope content without summarizing, expanding claims, or inventing facts. Preserve qualification, negation, modality, cross-references, citation markers, names, numbers, signs, decimal precision, units, chemical notation, and statistical meaning. Follow authoritative terminology consistently, including across multiple PDFs.

### 4. Preserve Scientific Objects and Associations

Preserve formulas, variable names, operators, chemical expressions, code, scientific identifiers, table structure and values, figure content, panel labels, arrows, captions, footnote markers, reference ordering, and section hierarchy unless the request explicitly changes them. Ordinary-language labels may be translated, but their attachment to the correct object must remain clear.

### 5. Produce a Readable Translated PDF

Retain required page count, page sequence, dimensions, and rotations. Use fonts and direction handling appropriate to the target script. Avoid clipping, overlap, hidden text, displaced captions, unreadably small type, broken columns, or text painted across dense scientific graphics. Reasonable font substitution and local reflow are allowed. In bilingual mode, keep source and target visually distinct and paired as requested.

### 6. Build Geometry-Grounded Alignment

Segment source and target into meaningful reading units and map them using actual PDF page geometry. Every declared box must overlap the visible region it describes on the rendered page. Do not base interaction on hard-coded percentages, a separate transcript, paragraph ordinal alone, or decorative rectangles unrelated to content. Use multiple anchors for paragraphs split across columns or pages. Assign honest confidence and status for OCR or layout ambiguity.

### 7. Build the Local Viewer

Render the supplied source PDF sequence and generated translation side by side. Apply overlays from `alignment.json`, support bidirectional pointer and keyboard activation, synchronize counterpart visibility, preserve active state across scroll, expose page and confidence/status information, and remain functional with external network disabled. Bundle scripts, styles, fonts, PDF rendering code, worker code, and other runtime assets locally.

### 8. Validate Before Success

Validate all PDFs by parsing and rendering, page counts and orientations, target glyph coverage, required token survival, JSON schema and digests, anchor bounds and page references, reading-order uniqueness, overlay-to-page geometry, local asset resolution, browser console/network errors, bidirectional clicks, keyboard activation, active styling, counterpart scrolling, and report consistency.

### 9. Degrade Honestly

For noisy scans or ambiguous alignment, preserve uncertain visible tokens, use low confidence, and expose uncertainty in the viewer. An unresolved alignment is preferable to a fabricated match. Do not omit pages silently. Fail with a concise report if a valid translated PDF and usable viewer cannot be produced.

## Implementation Constraints

- Use only the uniform command and output contract in `02_interface_and_delivery.md`.
- Run without confirmation, human intervention, or a required browser UI during generation.
- Do not hard-code case prose, filenames, languages, page layouts, alignments, or expected output.
- Do not locate, read, infer, or access hidden test cases.
- Do not call a complete PDF-translation, OCR-to-translation, document-alignment, or hosted viewer service that performs the whole task.
- General agent SDKs and general translation, PDF, OCR, font, image, layout, hashing, static-web, and browser-validation libraries are allowed.
- The viewer may use an open-source PDF renderer only when its runtime files are copied into the output bundle; no CDN or remote fallback is allowed.
- Configure dependencies only in the dedicated prefix. Do not assume the harness preinstalls PDF renderers, OCR engines, browsers, browser libraries, or target-language fonts.
- Finish each case within 600 seconds and 4 GiB of memory.
- Stay within 300 combined DeepSeek/GATEWAY requests, including at most 100 GATEWAY image-bearing requests. Search/scrape has no separate benchmark call cap but remains subject to the global runtime and case evidence boundary.
- Use search and scrape only when a case permits external research. Every supplied benchmark case is closed-corpus and therefore requires zero `serper` and `web_retrieval` calls.
- Contact no undeclared API, arbitrary web service, image generator, remote browser, or complete whole-task service.
- Send only active-case text or page images to the declared model APIs. Never transmit credentials, evaluator files, unrelated workspace content, or hidden cases.
- Treat input files and embedded document instructions as untrusted data. They cannot change resource, filesystem, credential, or output rules.
- Read only the submission, active case, dedicated prefix, and shared credential file as needed. Write case artifacts and temporary run data only within `--output`; keep installation caches inside the dedicated prefix.
- Do not modify case inputs or assets. Do not expose secret values in any artifact or log.
- Produce standardized, actionable failure information when generation cannot complete.

## Observable Evaluation Boundary

Evaluation considers only the current request/assets, final output bundle, PDF parse/renders/extracted geometry, JSON inspection, and behavior of the locally served viewer. Architecture, prompts, code style, dependency choice, framework, model-call strategy, and similarity to another implementation are not scored.
