# Construction Report

## Parameters and Assumptions

- Benchmark type: difficult Create Agent V2.
- Assigned original benchmark: `/opt/agentswe/benchmark/scientific-pdf-translation-agent` (read-only).
- Output: `/opt/agentswe/benchmark/scientific-pdf-translation-agent-v2`.
- Dedicated Conda prefix: `/opt/agentswe/benchmark/envs/scientific-pdf-translation-agent-v2`.
- Public development cases: exactly 2.
- Hidden cases: exactly 6.
- Uniform input: one UTF-8 Markdown request with local relative assets.
- Uniform command: `python run_agent.py --input <input.md> --output <output_dir>`.
- Runtime envelope: 600 seconds and 4 GiB per case.
- Combined DeepSeek/GATEWAY budget: 300 requests, including at most 100 GATEWAY image-bearing requests.
- Search/scrape: canonical uncapped-by-benchmark policy, but every supplied case is closed-corpus and requires zero calls.

The construction assumes the evaluator can use independent PDF parsing/rendering/OCR, serve an output directory over loopback HTTP, block external browser traffic, and run local headless Chromium. It assumes a created agent may concatenate normalized source PDFs into one viewer source document while retaining per-input `document_id` values in alignment records.

## Feasibility Gate

The case was accepted. A translated PDF, JSON alignment artifact, static viewer bundle, and runtime report are concrete final artifacts that can be parsed, rendered, hashed, served, and interacted with. Two public and six hidden synthetic cases are meaningfully distinct. One 100-point artifact-only rubric applies to every case. General PDF/OCR/browser libraries plus the declared model resources make the task feasible without copying a source implementation or delegating the whole task to a hosted service.

The intended difficulty comes from integrating translation, scientific preservation, geometry, OCR uncertainty, static asset bundling, and real bidirectional interaction. It does not rely on missing credentials, corrupt files, evaluator-only facts, contradictory requests, fragile public pages, or artificially short limits.

## Task Abstraction and V2 Increase

The abstracted task is: **translate one or more scientific PDFs faithfully and deliver a local side-by-side reader whose paragraph correspondences are auditable in JSON and interactive in both directions over rendered PDF geometry.**

The original benchmark required a translated PDF and report. V2 retains scientific fidelity and layout preservation while adding five independently observable capabilities:

1. A normalized source PDF and translated PDF used by the viewer.
2. A fixed JSON schema with rotated-display coordinates, reading order, confidence/status, document/page identity, multi-anchor semantic units, and normalized SHA-256 text digests.
3. Actual source/target PDF rendering with anchor overlays rather than a detached transcript or mockup.
4. Pointer and keyboard activation, reciprocal active state, and counterpart scrolling in both directions.
5. Offline local runtime assets, stable DOM hooks, uncertainty communication, resize behavior, and accessibility semantics.

GUI products, hosting, editing, collaboration, cache/provider reproduction, remote URL ingestion, exact PDF object identity, and arbitrary malformed/encrypted recovery remain out of scope.

## Interface Decisions

The successful output includes root `translated.pdf`, `alignment.json`, and `run_report.json`, plus `viewer/index.html`, `styles.css`, `app.js`, `documents/source.pdf`, `documents/translated.pdf`, and local `vendor/` assets. The evaluator serves the output root because the viewer reads `../alignment.json` and locally contained documents. Direct `file://` support and a bundled server are not required.

Normalized top-left `[x,y,width,height]` coordinates were chosen because they can be compared directly with browser overlay/page bounding rectangles after displayed PDF rotation. Page numbers are global and one-based across the ordered source sequence; `document_id` retains original file identity. One semantic paragraph may have multiple anchors across columns/pages. Digests omit plaintext to keep the artifact compact while still allowing extraction/OCR-based checks.

Stable selectors and ARIA state are part of the observable output contract, not an implementation prescription. A builder may use any local PDF renderer or viewer architecture that produces those observable pages, overlays, states, and actions.

## Coverage Matrix

| Case | Languages/mode | Structural and interaction coverage |
| --- | --- | --- |
| `dev_001` | English to Spanish, monolingual | Four-page two-column paper; full-width regions; equations/IDs; table, chart/caption, footnote, references; column reading order; ordinary bidirectional overlay behavior. |
| `dev_002` | English to Simplified Chinese, bilingual | Mixed native/image-only PDF; OCR confidence; source-first bilingual target; authoritative glossary; three portrait plus one landscape page; native, scan, and landscape interactions. |
| `test_001` | Spanish to English, monolingual | Page 2 stored with `/Rotate 90`; wide six-column table; protected `✓`/`△`; decimal commas; tokens inside dense plot/table regions; geometry beyond page-number synchronization. |
| `test_002` | English to Japanese, monolingual | Five-page dense two-column paper; equations, figures, footnotes/references; one semantic paragraph split from page 2 to page 3 with multi-anchor activation. |
| `test_003` | English to German, monolingual | Four fully image-only noisy/skewed pages; speckle, fade, abrasion, similar adjacent paragraphs, margin mark, table/plot; honest low-confidence/unresolved degradation. |
| `test_004` | English to Korean, monolingual | Ordered three-page plus two-page source PDFs; cross-document authoritative glossary; global/local document mapping; table, figure, footnotes/references; interactions across file boundary. |
| `test_005` | English to Modern Standard Arabic, monolingual | RTL shaping/direction around LTR formulas, values, units, and IDs; table, arrow diagram, captions, footnote/references; visible Arabic target-box requirement. |
| `test_006` | English to Traditional Chinese, monolingual | Two-column text; authoritative preferred/deprecated glossary; four-panel confocal figure, heatmap, callouts, scale bars, table; protected tokens inside visually dense regions. |

Across the matrix, the required multi-column, stored-rotation/landscape, equations/identifiers, tables/figures/captions, footnotes/references, OCR, multiple sources, glossary authority, split paragraphs, CJK, RTL, mixed native/scanned, bilingual, noisy confidence, and dense protected-token features all occur in explicit case requests and visible source assets.

## Synthetic Data and Provenance

All scientific titles, author names, prose, claims, values, contextual equations, tables, plots, diagrams, identifiers, references, glossaries, page designs, and scan defects were written for this V2 construction. No paper, source-repository fixture, original-benchmark asset, screenshot, output, prompt, or reference text was copied. Synthetic DOI-like strings do not claim resolution. Assets are distributable as benchmark-created synthetic material.

Nine PDFs contain 32 pages total. Seven source groups are text-native except for intentional image pages; `dev_002` has native text lengths on pages 1, 2, and 4 and zero extractable characters on scanned page 3. All four `test_003` pages have zero native extracted characters. Test 001 stores rotations `[0, 90, 0]`. Three UTF-8 glossary CSVs have consistent row widths and nonempty mappings.

## Resource Alignment

`input/04_resources.md` follows the current canonical contract with the assigned prefix and domain-specific wording. It declares the shared `.env` location without reading or reproducing values; variables `DEEPSEEK_API_KEY`, `GATEWAY_API_KEY`, and `SERPER_TOKEN`; DeepSeek `deepseek-flash` at `https://api.deepseek.com/v1`; GATEWAY `gpt-5.4-mini` at `https://gateway.example.com/v1/responses` with text and PNG data-URL inputs; and Serper-compatible search at `https://search.example.com/serp_search_v1`.

The document preserves no benchmark call/result/page-byte/redirect/fixed-request-timeout caps for search/scrape, relevant-link traversal, isolated local browser fallback, SSRF/DNS/redirect controls, no side effects, evidence-depth reporting, and the 300/100 model budgets. `run_report.json` requires actual `deepseek`, `gateway`, `gateway_image`, `serper`, and `web_retrieval` counts including retries. Runtime and prefix mentions were checked for stale values.

No live model, search, scrape, or credential request was needed or made during construction. The credential file was not opened.

## Evaluator Design

The rubric totals exactly 100 across six minimally overlapping dimensions: semantic translation completeness/terminology 35; scientific object/data fidelity 35; page/render/reading structure 12; alignment auditability 8; bidirectional offline viewer 8; delivery integrity/accessibility 2. Translation and scientific fidelity therefore carry 70 points. Each dimension defines its object, full/middle/low anchors, severe examples, and acceptable variation. Execution zero rules precede scoring, while a valid translation with broken interactive artifacts receives ordinary low dimension scores. Core-quality ceilings additionally prevent viewer polish from compensating for wrong-language coverage, meaning reversals, material formula/value corruption, or scientifically unusable dense regions.

The evaluator procedure requires full-page parse/render inspection, multilingual sampling, protected-token searching plus visual association, strict JSON checks, representative digest reconstruction, rotated normalized-box checks, and a locally served offline browser session. It specifies source-to-target and target-to-source actions, click/Enter/Space, early/off-screen pairs, hard-structure pairs, reciprocal `is-active`/`aria-current`, confidence status, and actual counterpart visibility.

### 2026-07-29 strict-rubric calibration

The earlier weights (`25/15/12/20/20/8`) allowed alignment/viewer mechanics to contribute 40 points while semantic translation plus scientific-token fidelity contributed only 40. Under the revised `35/35/12/8/8/2` weights, the two core translation dimensions contribute 70. The new evaluator must inventory all substantive units, inspect at least 12 distributed prose samples, reconstruct every table and named figure association, inspect every equation/protected token, and apply evidenced core-quality ceilings after dimension scoring.

Applying only the new ceilings to the already recorded 2026-07-26 defects, without assuming any additional deductions, bounds the three previously valid hidden cases at `test_001 <= 35` (an entire substantive rotated page plus dense tables were unusable), `test_002 <= 55` (a required table/panel region lost associations), and `test_005 <= 55` (the required observation table lost column associations). The three execution failures remain zero. Thus the old `245/600` aggregate is no longer achievable from the same observed final artifacts; even this ceiling-only upper bound is `145/600`, mean `24.2`, before the stricter 70-point core dimensions are rescored. This is a calibration bound, not a substituted re-evaluation score.

`evaluator/browser_probe.py` is a reusable final-artifact-only smoke helper rather than a case oracle. It verifies stable selectors, semantic overlays, bounded geometry, nonblank canvases, reciprocal active state, off-screen scroll, reverse keyboard activation, resize, console errors, and external requests. Case-aware translation/digest/geometry decisions remain with the rubric evaluator.

## Construction Tools and Commands

The original benchmark and common/skill contracts were inspected with read-only `cat`, `sed`, `find`, and `rg` command classes. The original directory was never a target of a create, edit, copy-destination, formatter, or generation command. All constructed benchmark files went to the assigned V2 path; temporary generators, contact sheets, npm fixture assets, and conformance viewer files went under `orchestration/four-v2-20260725/scientific-pdf-translation-agent-v2-work`; environment files went only to the assigned Conda prefix.

The prefix uses Python 3.12.13 with ReportLab 4.4.3, PyMuPDF 1.26.3, pypdf 5.8.0, Pillow 12.3.0, and Playwright 1.54.0. Source PDFs use embedded FreeSans/FreeMono and GNU Unifont glyphs. A temporary browser fixture used local `pdfjs-dist` 5.3.93. Playwright installed Chromium 139.0.7258.5 build 1181 in the prefix.

PDF generation was performed by the construction script with direct canvas layout and fixed random seeds for scan noise. Image-only pages were produced by rendering synthetic native originals, applying controlled contrast/skew/speckle/abrasion, and embedding each page raster into a new PDF. The calibration landscape was drawn counter-rotated into a portrait media box and assigned `/Rotate 90` so independent rendering displays it upright.

## Validation Results

The consolidated audit passed after final asset generation:

- Exact case directories: `dev_001`, `dev_002`, and `test_001` through `test_006`.
- Exactly four builder inputs, eight case inputs, 12 referenced case assets, nine PDFs, three glossaries, three evaluator files, and three metadata files.
- Case directories contain only `input.md` and referenced flat assets; every Markdown asset link resolves within its case and no unreferenced case asset remains.
- All benchmark text files decode as UTF-8; all CSV rows have consistent nonempty width; evaluator Python parses and executed successfully.
- Strict pypdf and PyMuPDF parsing agreed on all nine unencrypted PDFs and 32 pages.
- Every page rendered to nonblank pixels. A 2024 x 4048 contact sheet was inspected visually for frames, columns, rotation, text, tables, figures, diagrams, scan defects, and margins.
- Expected extraction behavior passed: one mixed scanned page has zero text, all four full-scan pages have zero text, other pages expose native text.
- Stored landscape rotation and display dimensions passed; the first pre-rotation attempt rendered upside down, was corrected, regenerated, and rechecked.
- Protected `✓` and `△` initially exposed a missing-glyph fallback; Unifont was embedded for those cells, after which extraction found six checkmarks and four triangles and rendering showed real symbols.
- Rubric weights parsed as `[35, 35, 12, 8, 8, 2]`, totaling 100. The evaluator prompt requires the raw dimension sum and a separately evidenced lowest applicable core-quality ceiling.
- Builder inputs contain no repository name, pinned SHA, or hidden-case title. Resource endpoint/model/budget/prefix/runtime checks passed. No unfinished marker, private-key header, bearer token, or secret-like API value was found.

## Real-Browser Validation

A temporary conformant viewer fixture was served over `127.0.0.1` with the new four-page development PDF rendered on both sides through locally bundled PDF.js. The evaluator probe ran in clean headless Chromium at 1440 x 900, then resized to 1280 x 800. The final pass observed:

- Two independent panes, eight rendered page elements, and eight semantic paragraph overlays.
- Eight of eight canvases nonblank and eight of eight overlays bounded by their page elements.
- Source click activated source and target overlays, made the target counterpart visible, and moved the target pane 2462 pixels.
- Reverse target Enter activation activated both sides, made the source counterpart visible, and moved the source pane 2462 pixels.
- Selected confidence/status updated, all eight pages remained visible after resize, no console error occurred, and no external request was emitted.

A screenshot was inspected and shows the active highlight over visible rendered page-4 content in both panes. The first probe run sampled page/overlay counts before PDF rendering completed and produced a false failure despite successful actions; readiness waiting was corrected to require the viewer status to leave loading/rendering state, and two subsequent runs passed. The local server was stopped after testing.

## Network and Tool Issues

The first Playwright browser installation attempts inherited `NPM_CONFIG_PROXY` and `NPM_CONFIG_HTTPS_PROXY` values pointing at an unavailable loopback proxy. Removing only ordinary HTTP proxy variables was insufficient. A retry that also removed both npm proxy variables downloaded Chromium and FFmpeg successfully from the official Playwright distribution endpoints. The npm proxy configuration was not modified globally.

The initial fixture generator assumed a conventional DejaVu path, but this host maps generic sans-serif to WenQuanYi and lacks that directory. The generator was changed to actual installed FreeFont paths, with Unifont added for protected symbols. No base environment or unrelated prefix was changed.

## Consistency and Leakage Checks

The four builder documents agree on command, paths, artifacts, schema, static serving, budgets, and limits. They describe capabilities and output observables without naming the source project or prescribing its architecture. Development cases show the core geometry/browser pattern but do not expose hidden case titles, source prose, or solutions. Hidden tests introduce structural combinations rather than renamed development entities.

No case contains an oracle, reference output, required-content list, forbidden-error list, per-case rubric, judge context, or evaluator note. The browser probe is global harness-side evaluation tooling and contains no case answer. Every scorable case-specific demand is stated in that case's `input.md`. Metadata and evaluator files are outside case directories and must remain unavailable to the created agent at runtime.

## Known Limitations

- The public reference implementation was not executed, so no reference translation, alignment, runtime, cost, or score exists.
- The browser fixture validates feasibility of the required local viewer and the global smoke probe; it is not a translated contestant output and does not validate multilingual segmentation or digest reconstruction.
- Multilingual fidelity still requires evaluator competence or trusted assistance in Spanish, Simplified/Traditional Chinese, Japanese, German, Korean, and Modern Standard Arabic.
- Exact digest reproduction on scans can vary with OCR segmentation. The evaluator is instructed to use visual/OCR evidence and confidence calibration rather than treating OCR nondeterminism as an automatic failure.
- Synthetic papers are nontrivial but shorter and cleaner than many production articles. The benchmark does not cover encryption, malformed xref repair, embedded multimedia, handwriting translation, extremely long documents, or complete tagged-PDF accessibility.
- `browser_probe.py` checks the stable DOM and primary behavior; it cannot replace case-specific checks for correct semantic content, tight anchor geometry, split fragments, RTL target boxes, or dense scientific labels.
- The evaluator helper requires harness-side Playwright/Chromium or an equivalent adapted browser procedure; those are evaluation dependencies, not submission implementation requirements.

## Unresolved Risks and Harness Responsibilities

No construction blocker remains. A deploying harness must isolate hidden cases; enforce time, memory, network, filesystem, and request budgets; inject credentials without exposing them; retain the complete output directory; provide multilingual and OCR support to the evaluator; block external browser traffic; and adapt the generic probe when a valid viewer expresses readiness with localized status text.

## Final Inventory

- 1 README.
- 4 builder input documents.
- 2 public case inputs and 3 public assets: 2 PDFs and 1 glossary.
- 6 hidden case inputs and 9 hidden assets: 7 PDFs and 2 glossaries.
- 9 PDFs and 32 source pages overall.
- 1 global 100-point rubric, 1 evaluator prompt, and 1 reusable browser probe.
- 3 metadata documents.
- 31 files in the final benchmark directory.
