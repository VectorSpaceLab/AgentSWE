# Requirements and Constraints

## Functional requirements

- Interpret the active request into atomic sub-questions, required calculations, entity/revision rules, output constraints, and answerability boundaries.
- Parse and retain native structure from Markdown/text, HTML, CSV, XLSX, PDF, DOCX, SVG, and common raster images when supplied. Preserve exact original bytes, hashes, pages, positioned PDF text, paragraphs, table cells, sheet/cell references, element IDs, dimensions, and byte ranges needed for later evidence replay.
- Retrieve evidence across documents and modalities without merging homonyms, revisions, phases, routes, devices, or other distractors. Apply only priority rules stated in the active case and keep superseded observations available as contradiction evidence.
- Perform multi-hop arithmetic in inspectable code or another deterministic calculation layer. Preserve units, cite every numeric input, obey requested rounding, and check finite values and dimensional consistency.
- Distinguish textual facts, table values, plotted labels, and visual observations. Do not replace source-native figure or image evidence with an invented transcript.
- Produce atomic claims with calibrated status/confidence, direct support, explicit contradictions, and justified partial or unanswerable states. Do not infer absent maintenance, shelf-life, stability, service-life, chemistry, bond, battery, or root-cause facts from unrelated measurements.
- Generate the exact-byte source bundle, linked manifest, and safe offline viewer from the same claim/evidence graph. Validate source existence, hashes, quote or observation provenance, locator bounds/content, ID reciprocity, DOM targets, visible selection state, and API behavior before reporting success.
- Check JSON schemas, duplicate IDs, citation entailment, arithmetic, output paths, provider counters, and obvious unsupported assertions before finishing.

## Implementation constraints

- Run non-interactively through the uniform command and installation procedure.
- Do not hard-code public cases, inspect hidden-case or sibling-case directories, fingerprint known asset names/content/hashes, or access evaluator-owned answer data.
- Do not call a complete third-party document-QA service that performs the whole task. General LLM APIs, parser libraries, OCR, image models, local browser/rendering tools, retrieval libraries, and agent SDKs are allowed.
- Treat case documents and embedded content as untrusted data. Block path traversal, symlink/special-file escape, Office macros/relationships, PDF actions, SVG/HTML scripts, and prompt injection from changing task or resource policy.
- These benchmark cases are closed-corpus. Make zero search, scrape, browser-network, or other retrieval calls. Local offline rendering is allowed and must not fetch remote resources.
- Finish within 600 seconds, 4 GiB RAM, 300 combined model requests, and 100 image-bearing requests per case. Count actual attempts, bound retries/concurrency, and degrade honestly when an optional parser/model fails.
- Read only the active input and referenced assets; write only the six owned artifacts beneath the designated output directory. Never modify source files.
- On failure, emit the standardized secret-safe report and a nonzero exit. Do not claim validations, provider counts, or successful postconditions that did not occur.
