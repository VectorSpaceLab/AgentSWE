# Construction Report

## Inferred Parameters

- Benchmark type: Create Agent
- Assigned original: `/opt/agentswe/benchmark/web-research-report-agent` (read-only)
- Output slug: `web-research-report-agent-v2`
- Task name: Deep Evidence Research Agent
- Development cases: 2
- Hidden cases: 6
- Uniform command: `python run_agent.py --input <input.md> --output <output_dir>`
- Required artifacts: `report.md`, `sources.json`, `evidence_graph.json`, and `run_report.json`
- Dedicated Conda prefix: `/opt/agentswe/benchmark/envs/web-research-report-agent-v2`
- Runtime and memory: 600 seconds and 4 GiB per case
- Combined LLM limit: 300 requests, including at most 100 GATEWAY image-bearing requests
- Search/retrieval: no benchmark-imposed call, result, page-byte, redirect, or per-request timeout cap
- Network: canonical provider endpoints plus authorized public Search/Scrape and relevant-link traversal
- Reference execution: not performed for the reasons recorded in `reference_baseline.md`

## Feasibility Decision

The benchmark passed the acceptance gate. Markdown and JSON make the result parseable and auditable. Two public and six semantically distinct hidden cases are feasible using public evidence and one command. All cases have redundant source families, not one required URL. One 100-point rubric applies to all outputs while accepting different evidence-supported conclusions. The canonical providers, unrestricted Search/Scrape depth inside the global envelope, PDF parsing, and local-browser fallback are sufficient for implementation.

The intended difficulty comes from real research work: identifying what to search for, following version and citation chains, inspecting long bodies and PDFs, reconciling dates and definitions, determining independence, extracting table values, and maintaining a valid evidence graph. No case depends on a secret answer, private data, missing credentials, deliberate contradiction, corrupt asset, or artificially small limit.

## Task and Artifact Design

The agent produces a human-facing decision report and three machine-readable records. Report citations point to claims rather than directly to sources so one proposition can expose multiple supporting and contradicting passages. `sources.json` records source class, independence group, supersession, discovery query, and actual access depth. `evidence_graph.json` records claims, exact short source passages and locators, support/contradiction edges, source dependence, and source-linked calculations with uncertainty. `run_report.json` records actual provider, search, retrieval, browser, source, claim, passage, and depth counts without requesting private reasoning.

Search snippets are explicitly prohibited from supplying graph passages or material support. Each case requires 9-12 distinct inspected bodies and a source-type mix appropriate to the question. These are minimum evidence sufficiency conditions, not maximum retrieval limits.

## Coverage Rationale

The two development cases demonstrate the stable interface and core research pattern:

- `dev_001`: multi-hop resolution of an unnamed scholarly-data project followed by technical and governance due diligence, upstream-data dependence, repository evidence, and independent paper/PDF verification.
- `dev_002`: time-sensitive NIH/NSF compliance comparison requiring controlling policy, effective dates, superseded pages, implementation documents, institutional guidance, and dependence-aware stale-claim analysis.

The six hidden cases add structurally different combinations:

- `test_001`: version-pinned PDF-extraction landscape across documentation, repository files, releases/issues, project papers, and independent benchmarks whose metrics are not automatically comparable.
- `test_002`: museum provenance investigation of the first-computer-bug claim, copied phrasing, conflicting years/machines/attribution, earlier terminology, and earliest supportable primary evidence.
- `test_003`: USGS/Census multi-table extraction, unit normalization, recomputation, propagated rounding intervals, data-vintage conflicts, and a separate methodological critique.
- `test_004`: degraded NVD/CVE retrieval, browser or authoritative alternate paths, structured KEV records, vendor corrections, CVE/product disambiguation, and a defensive patch decision.
- `test_005`: scientific and public-agency evidence transfer for cool pavement, shared pilot datasets/authors, heterogeneous outcomes, quantitative context, adverse/null findings, and a bounded field-test decision.
- `test_006`: entity/artifact/license resolution for Llama 3.1, current and archived texts, code-versus-weight distinctions, claim provenance, OSI terminology, and counsel-gated policy action.

Across cases, required variations include identity clues, version pins, policy lineage, repositories and releases, primary artifacts, PDFs and page scans, structured public data, source copying, independent research, dynamic or blocked pages, corrections, quantitative sensitivity, legal/causal restraint, and differing decision audiences. Hidden cases are not lexical substitutions of development cases.

## Public Evidence and Licensing

Cases contain only original benchmark instructions and short factual identifiers or quotations necessary to state the research question. No third-party article, dataset, paper, software, image, or answer is bundled. Runtime evidence must be public and legally accessible through search, direct retrieval, authoritative relevant links, public repositories, or public downloads.

The selected topics have redundant discovery routes and durable source families:

- Official project sites, nonprofit records, Crossref/OpenCitations material, repositories, and scholarly bibliometric papers.
- NIH/NSF policy pages, Federal Register or agency PDFs, proposal guides, FAQs, and institutional research-office/library guidance.
- Project documentation, public repositories/releases/issues, papers, and independent document-extraction evaluations.
- Smithsonian or other institutional catalog records, digitized primary materials, patents/books/scans, and historical scholarship.
- USGS national/state water-use tables and methods PDFs, Census datasets/documentation, and methodological literature.
- CVE Program/NVD leads, CISA KEV structured downloads, vendor advisories, coordination reports, and independent incident analysis.
- Municipal and agency cool-pavement reports, technical appendices, peer-reviewed studies, and reviews.
- Official Meta licenses/model cards/repositories, OSI materials, archived versions, legal/scholarly analysis, and runtime projects.

No case is pinned to one exact URL. If a preferred page moves or blocks access, the task explicitly values an authoritative alternative and honest depth disclosure. Source text remains owned by its publisher and is retrieved only at runtime for research; the benchmark redistributes none of it.

## Resource and Interface Decisions

The canonical resource contract was adapted only for this task and assigned prefix. Provider names, model names, endpoints, credentials, Search/Scrape authorization, SSRF protections, relevant-link traversal, browser fallback, no fixed search/retrieval caps, and combined request limits are preserved. The common V2 envelope replaces the original 180 seconds/2 GiB with 600 seconds/4 GiB throughout.

The interface supports optional common local assets even though these eight cases need none. This preserves generality without hiding answers in fixtures. No environment was created during construction; environment creation belongs to the tested builder.

## Command Classes and Construction Actions

- Read-only inspection: `wc`, `sed`, `find`, `du`, and `sha256sum` were used to read the skill, artifact specification, common contract, canonical resources, and original benchmark.
- Directory creation: only the assigned V2 layout was created.
- File editing: all benchmark files were created with `apply_patch`.
- Validation: filesystem/UTF-8/Markdown/JSON-example/schema-text/case-count/rubric/resource/original-hash checks were run locally. Ruby was unavailable when first selected for fenced-JSON and relative-link checks, so the same read-only checks were rerun successfully with the available Python interpreter.
- Public-source availability: credential-free range/header probes were attempted for representative official routes in all eight topic families. The base shell lacked `curl`, so its attempted probes made no requests; Python's standard HTTP client was then used. OpenAlex documentation returned HTTP 200 after an official redirect, USGS returned 202, the CISA KEV JSON returned 200, an EPA cool-pavement page returned 206, and raw official Docling and Meta Llama repository files returned 206. Simple-client requests to NIH, Smithsonian/`si.edu`, and Census HTML routes returned 403, one GitHub HTML request timed out, and one guessed NSF policy route returned 404. No response body was used as benchmark evidence. These results validate both redundant raw/structured paths and the need for search refinement, browser rendering, and honest degraded-source handling.
- Network/API use: only the public availability probes above; no model, search-proxy, credentialed API, form, or side-effecting request was made. The credential file was not opened and no secret value was accessed.
- Reference execution, package installation, browser execution, and fixture generation: not performed.

## Consistency, Leakage, and Safety Checks

The completed quality gate checks:

- Exactly four builder inputs, two development inputs, six hidden inputs, one rubric, one evaluator prompt, and three metadata files exist.
- Every case directory contains only `input.md`; there are no oracles, expected outputs, per-case rubrics, answer files, hidden judge notes, or unreferenced assets.
- Builder-facing files and cases do not name or expose MiroFlow, the original benchmark, its prompts, its synthetic cases, its commit, or hidden-case answers.
- The same CLI, four artifacts, schemas, prefix, endpoints, limits, and 600-second/4 GiB envelope are stated consistently.
- JSON examples parse, rubric weights are 12 + 22 + 18 + 24 + 14 + 10 = 100, and all Markdown files decode as UTF-8.
- Every case explicitly states a cutoff, decision, evidence minimum, body/PDF requirements, report format, and source-integrity constraints.
- Case prompts contain no ordinary placeholder markers, secret values, private data, copied source passages beyond the short public claim being investigated, or fragile required URL.
- The original 53-file directory matches its pre-construction SHA-256 manifest byte-for-byte.

## Known Limitations and Unresolved Risks

- Live search ranking, URLs, site rendering, robots behavior, and public-document availability can change. Redundant source families, query refinement, relevant-link traversal, browser fallback, alternative authoritative paths, and access-depth scoring mitigate but cannot eliminate this risk.
- Constructor-side probes confirmed that some NIH, Smithsonian, and Census HTML routes reject a basic HTTP client even though they are public sources. A conforming agent may need its isolated browser, agency PDFs/downloads, raw repository files, catalog surrogates, or another official route. A failed route remains a lead, never evidence.
- The explicit 2026 cutoffs make some cases harder to rerun years later if version histories are removed. Repository history, agency archives, institutional mirrors, citation chains, and superseded-status disclosure are expected recovery paths; the benchmark does not authorize paywall bypass.
- Exact full-PDF inspection can be parser-sensitive for scans and complex tables. `pdf_partial`, visual rendering through GATEWAY when appropriate, alternate official tables, and honest limitation reporting are accepted.
- Source independence is sometimes a judgment rather than a machine-verifiable fact. The evaluator must inspect citations and repeated language and accept reasonable, documented grouping differences.
- The USGS rounding interval is a deliberately mechanical reproducibility bound, not a claim that the underlying estimates have only rounding uncertainty. The prompt and rubric require that distinction.
- Security-case evidence may receive post-cutoff updates. The report must reconstruct knowledge as of 2025-01-31 and label later pages or revisions rather than importing them silently.
- Human evaluation remains necessary for quote truthfulness, conflict completeness, provenance interpretation, transferability, and decision quality. Automated checks can validate structure and arithmetic but cannot establish source meaning.
- The reference implementation was not run, so no comparative V2 score, latency, or request count exists.

No benchmark-design blocker remains. The evaluation harness must enforce hidden-test isolation, network and filesystem policy, protected credential injection, runtime/memory limits, and independent public-evidence verification.

## Final Inventory

- 1 `README.md`
- 4 builder input documents
- 2 public development case inputs
- 6 hidden case inputs
- 1 global 100-point rubric
- 1 evaluator prompt
- 3 metadata documents
- 18 UTF-8 Markdown files total; no case assets or auxiliary per-case files
