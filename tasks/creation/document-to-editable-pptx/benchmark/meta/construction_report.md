# Construction Report

## Parameters

- Benchmark type: Create Agent.
- Case slug: `document-to-editable-pptx-agent-v2`.
- Assigned source benchmark: `document-to-editable-pptx-agent` (preserved read-only).
- Output directory: `/opt/agentswe/benchmark/document-to-editable-pptx-agent-v2`.
- Dedicated Conda prefix: `/opt/agentswe/benchmark/envs/document-to-editable-pptx-agent-v2`.
- Public development cases: exactly 2.
- Hidden test cases: exactly 6.
- Uniform command: `python run_agent.py --input <input.md> --output <output_dir>`.
- Required artifacts: `deck.pptx`, `source_manifest.json`, and `run_report.json`.
- Per-case envelope: 600 seconds, 4 GiB, at most 300 combined DeepSeek/GATEWAY calls, and at most 100 image-bearing GATEWAY calls.

## Method and Command Classes

Construction read the complete benchmark-building skill, artifact specification, common V2 contract, original benchmark, and relevant local repository metadata before editing. Commands were limited to read-only file/tree inspection, checksums, Git metadata, public HTTP/search verification, directory creation, patch-based file creation, Conda-prefix creation, validation scripts, and SVG raster smoke tests. No reference code, installer, or untrusted repository script was executed.

The provisioned Serper token was loaded only into a process environment for the configured proxy request and was never printed. DeepSeek and GATEWAY were not called because construction did not require model generation or image analysis. No credential value was copied into V2.

## Task Abstraction

V2 changes the user value from instruction-to-slides into evidence-to-editable-presentation. The created agent must research when authorized, inspect source bodies, recover from blocked sources, resolve priorities/conflicts, recompute decision measures, create substantive visual explanations, preserve native PowerPoint objects, add notes and accessibility metadata, render its work, and ship an auditable claim/source/calculation manifest.

The interface remains one Markdown request with relative assets and one uniform command. Private reasoning and implementation details are explicitly excluded.

## Coverage Rationale

The two public cases reveal the core interaction pattern:

- `dev_001`: closed corpus, operational time series, aggregate calculations, a definition conflict, source priority, brand SVG, native chart/diagram, methodology notes, and accessible rendering.
- `dev_002`: live public research, primary-source depth, lawful NASA imagery or editable replacement graphics, recomputed period change, scientific caveats, native comparison, notes, and provenance.

The six hidden cases pressure distinct failure modes:

| Case | Professional deliverable | Distinct pressure |
|---|---|---|
| `test_001` | Historical AI-paper briefing | Discover a dated top three; distinguish rank from drifting engagement; verify through page bodies/redundant evidence; inspect papers; source or redraw visuals; compare honestly. |
| `test_002` | Filing/earnings briefing | Recover from a blocked SEC inline viewer; apply audited-source priority; recompute fiscal KPIs; distinguish segment/consolidated definitions; native financial charts. |
| `test_003` | Public incident/policy hearing | Enforce a historical evidence cutoff; separate verified facts from investigation; recover docket material; use public data; avoid fault/causality overreach. |
| `test_004` | Scientific landscape comparison | Reconcile lifecycle/system boundaries; refuse false comparability; communicate evidence strength; redraw mechanisms; make a research-portfolio synthesis. |
| `test_005` | Trilingual launch training | Preserve exact English/French/Arabic copy and RTL; recover from missing optional screenshots; native workflow; trainer notes; fixed three-column layout. |
| `test_006` | Noisy board decision | Resolve superseded/informal conflicts; aggregate a long pilot table; compute NPV/payback/sensitivity/weighted score; preserve hard gates and open risks. |

The suite varies research mode, source type, source count and length, audience, language, visual modality, calculation type, slide count, source conflicts, media availability, layout, notes, and decision stakes. Hidden cases are not renamed public cases.

## Historical Daily Papers Verification

The specified date `2025-05-29` was selected only after public-evidence checks:

- The exact Hugging Face dated page is indexed by the configured search service.
- Direct construction-shell access to Hugging Face timed out, establishing the need for redundant recovery rather than a single-page dependency.
- A public GitHub daily-paper archive has a pinned commit `7bccb9c36bef617bae6e24b8e1cd547d577e4320`, timestamped 2025-05-29 with commit message `Update daily papers 2025-05-29`; its full README body preserves an ordered dated list with paper and arXiv links.
- Search-indexed author/project evidence independently states a daily placement for that date, and individual paper pages and primary paper records remain discoverable.
- A second public dated-paper page returned a full HTML body during construction.

No paper title, top-three answer, rank mapping, engagement oracle, reference deck, or expected-output file is included in the hidden case or elsewhere in V2. The evaluator is instructed to reconstruct the answer from public evidence and distinguish dated order from access-time cumulative engagement.

## Data Provenance and Licensing

All Metrovale, Aster Field, and Cobalt Works organizations, facts, metrics, messages, translations, rollout data, quotes, risks, logos, and scenarios are synthetic benchmark material created for this case. The two SVG marks are original simple graphics. Synthetic assets may be used within the benchmark and generated outputs.

Live cases use public factual sources: NASA and U.S. agency material, SEC filings and issuer financial statements, official transportation/investigation records, public scientific assessments, and scholarly paper pages. The cases do not bundle copyrighted third-party figures. They require the runtime agent to verify and record the lawful-use basis for any acquired visual or create an original editable explanatory graphic.

## Resource Contract Decisions

The V2 resource file preserves the canonical endpoints, exact model names, credential variables, multimodal PNG data-URL transport, Serper proxy, direct `scrape(url)` semantics, relevant-link traversal, local headless-browser fallback, absence of benchmark-imposed search/result/page-byte/redirect/request-time caps, SSRF protections, and provider limits. Every runtime/resource mention was changed to 600 seconds and 4 GiB and points to the V2 prefix.

Actual provider counts, including zeros and the `gateway_image` subset, are required in `run_report.json`. Page access depth, unavailable leads, and retrieval notes are required in `source_manifest.json`.

## Rubric Design

The artifact-only rubric totals 100 points:

- Evidence correctness and analytical integrity: 25.
- Case compliance and narrative: 16.
- Visual storytelling and analytical representation: 17.
- OOXML validity, editability, and technical construction: 18.
- Provenance and auditability: 14.
- Rendered usability, notes, and accessibility: 10.

Validity rules precede scoring. The evaluator must render every slide, inspect OOXML chart/table/notes/alt-text structures, verify calculations and citations, and cite evidence for each deduction. Decorative polish cannot substitute for evidence.

## Network and Tool Observations

- `curl` was unavailable in the construction shell; `wget`, Conda, `unzip`, and `xmllint` were available.
- Direct Hugging Face HTTPS connections timed out in the construction shell. The configured Serper-compatible proxy returned indexed results, while GitHub API/raw content and a redundant dated-paper site were directly retrievable.
- Search and GitHub evidence was sufficient to verify date feasibility without embedding a hidden answer.
- No API call-limit or provider capability issue was observed. DeepSeek/GATEWAY were not probed in this V2 process; their confirmed canonical configuration is inherited from the required resource contract.

## Dependency and Fixture Assumptions

The builder may select Python and install an OOXML library, chart support, a local renderer such as LibreOffice, fonts including Arabic coverage, an HTTP/PDF stack, and optionally a local headless browser. The construction environment itself does not include LibreOffice, so no reference PPTX render was attempted. Harness-side evaluation must provide or install a stable renderer and independent OOXML inspection.

CSV and SVG fixtures are plain UTF-8 and deliberately small enough for the case envelope. The board pilot and cash-flow tables are long enough to require aggregation and selection but not large enough to create artificial memory pressure. Missing screenshots in the trilingual case are explicitly optional and recoverable.

## Consistency and Leakage Design

- Exactly four builder inputs use one interface and do not mention the source repository or hidden solutions.
- Every case contains only `input.md` and necessary user-facing assets.
- No per-case oracle, expected deck, rubric, answer, judge context, or evaluator note exists.
- Every scorable case-specific requirement appears directly in that case's input.
- No hidden paper ranking or filing calculation answer is stored in metadata or assets.
- Source IDs and calculation records are outputs to generate, not hidden answer keys.
- The original benchmark was checksummed before construction and will be checksummed again after validation to confirm byte preservation.

## Known Limitations and Unresolved Risks

- Live pages, cumulative upvotes, and public search indexes can drift. The dated archive and redundant-evidence requirement reduce but do not eliminate this risk; evaluator access timestamps must be retained.
- An SEC interface may behave differently across networks. The filing case explicitly treats the inline viewer as blocked and requires recovery from the official investor-hosted filed 10-K PDF, official results material, and retrievable archive documents, so success does not depend on reproducing the same HTTP error.
- Scientific estimates depend on system boundaries and evolving literature; the case rewards transparent non-comparability and evidence strength rather than a single expected chart.
- Font substitution can affect Arabic shaping and slide reflow. The builder must install appropriate fonts and render-preflight; evaluator-side font availability should be recorded.
- No reference agent was executed and no expected deck exists. Visual and accessibility scoring therefore requires evidence-backed evaluator judgment.

No benchmark-construction blocker remains. Deployment still must enforce hidden-case isolation, network/filesystem/resource limits, protected credential loading, and renderer/OOXML availability.

## Completed Validation

Validation was run after construction with the dedicated prefix's Python 3.11.15:

- Exact layout: 33 files; 4 builder inputs; 2 public cases; 6 hidden cases; 15 assets; 2 evaluator files; 3 metadata files.
- Case hygiene: every case has exactly one `input.md` plus referenced assets only; all 15 relative asset links resolve inside the active case; no orphan asset exists; no oracle/answer/rubric/judge-context filename appears in a case.
- Encoding and structure: every text file is UTF-8; all 7 CSV files parse with consistent columns and data rows; both SVG files parse as XML.
- Visual assets: both SVG marks were rasterized at 2x with CairoSVG and visually inspected for intact geometry, text, proportions, and canvas fit. Temporary PNGs are under `orchestration/four-v2-20260725/document-to-editable-pptx-agent-v2-smoke/`, outside the benchmark inventory.
- Rubric: six weights `[25, 16, 17, 18, 14, 10]` total exactly 100.
- Fixture semantics: 12 development months aggregate without zero denominators; all 13 trilingual copy keys have exactly English/French/Arabic rows; the board case has 36 pilot rows, 36 cash-flow rows, finite three-scenario NPVs, and a nontrivial hard-gate result with one eligible option.
- Resource consistency: no stale 180-second, 2 GiB, or V1-prefix runtime wording appears in builder/evaluator/README contracts; all relevant files state 600 seconds, 4 GiB, the correct models/endpoints, and the V2 prefix.
- Leakage and secrets: the four builder inputs contain no source-repository/test names; V2 contains no selected Daily Papers titles/IDs; no `.env`, key file, credential assignment, or secret-like token exists in V2.
- Live-source discovery: the configured search proxy returned primary-source results for NASA DART, the Apple filing, NTSB docket `DCA24MM031`, and DOE carbon-removal material. The SEC archive returned HTTP 403 in the construction shell, while Apple's official investor-hosted filed 10-K PDF and newsroom page were retrievable, validating the recovery path.
- Original preservation: the original still contains 47 files and no original file has a modification time after V2 construction began. Its final sorted file-checksum-list digest is `c45be1057e10d607b972d4ecd67174f4ff228ecf72e9c4f697ae3f840bcd739c`, consistent with the initial per-file checksum inventory.
- Environment: the dedicated prefix exists with 26 Conda package records and Python 3.11.15. CairoSVG and its Python dependencies were installed with `pip` only to smoke-test the two benchmark SVG assets. An initial `pip check` exposed unrelated host user-site packages; with `PYTHONNOUSERSITE=1`, the prefix reports no broken requirements and disables the user site. Builder, interface, resource, and README contracts now require that isolation flag. The future builder remains responsible for installing its complete presentation, browser, font, and rendering stack.

No deck was rendered during construction because this artifact is the benchmark specification, not a reference agent output, and LibreOffice is absent from the construction host. Rendering every produced test deck remains an explicit runtime/evaluator requirement.

## Final Inventory

- 1 README.
- 4 builder input documents.
- 2 public case inputs with 4 assets.
- 6 hidden case inputs with 11 assets.
- 1 global rubric and 1 evaluator prompt.
- 3 metadata documents.
- 33 files total.
