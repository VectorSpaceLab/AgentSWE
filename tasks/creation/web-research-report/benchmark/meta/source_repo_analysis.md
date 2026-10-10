# Source Repository Analysis

## Repository Identity

- Assigned read-only benchmark: `/opt/agentswe/benchmark/web-research-report-agent`
- Underlying public repository recorded by that benchmark: `https://github.com/MiroMindAI/miroflow`
- Owner: `MiroMindAI`
- Repository: `MiroFlow`
- Default branch: `main`
- Pinned reference commit: `fca7b4a7fda30e5a17b5c7b73d780738a925869f`
- Pinned tree: `2f4a3da555cf56968fda7cbce55aa3c20f74ff5e`
- Pinned commit date: `2026-01-30T10:09:01Z`
- License: Apache License 2.0
- Last repository metadata observation in the original analysis: 2026-07-24; GitHub `updated_at` was `2026-07-22T17:08:59Z`
- Original benchmark files inspected: 53

The original benchmark, not the public repository, was the assigned starting artifact for V2. It was inspected read-only and hashed before construction. Repository facts above are retained from its pinned source analysis rather than updated against a floating branch.

## Project Overview

MiroFlow is a Python research-agent framework and benchmark harness. Its public documentation and pinned code describe natural-language task interpretation, planning, optional main/sub-agent delegation, tool use, multiple LLM providers, web and document research, final synthesis, trace logging, and benchmark-specific short-answer extraction. It supports several difficult information-seeking benchmark configurations rather than one stable end-user report format.

The reusable capability is multi-step evidence acquisition and synthesis. The V2 benchmark abstracts that capability into a purpose-built agent whose evidence quality can be scored from final artifacts without observing its internal trajectory.

## Actual Reference Runtime

The pinned project expects Python 3.12 or later, `uv`, Hydra configuration, Fire command entry points, provider credentials, tool servers, and optional external services. `main.py` includes trace and benchmark commands. The trace pipeline builds configured LLM/tool clients and an orchestrator, then emits logged summaries, extracted answers, and structured traces.

Typical inputs are a natural-language task, optional file path, Hydra configuration, and credentials. Observable outputs include console summaries, benchmark answers, and detailed task traces. There is no single pinned, presentation-ready deep-research report plus claim graph matching this V2 contract.

## Core Capabilities Observed

- Intent recognition, decomposition, iterative tool use, and synthesis.
- Configurable single-agent or delegated research arrangements.
- Search, retrieval, and local-document reading.
- Optional Python execution, structured-data processing, visual analysis, OCR, audio, and video tools.
- Multiple LLM providers, retries, concurrency, context management, logging, and benchmark evaluation.
- Complex and multi-hop benchmark task configurations.

## Runtime Dependencies and Risks

The pinned dependency set includes Hydra, Fire, OpenAI and Anthropic clients, Google GenAI, E2B, MCP/FastMCP, document conversion, pandas, openpyxl, Pillow, datasets, HTTP clients, retry tooling, tokenization, and JSON/YAML libraries. Some configurations require credentials and services not present in this benchmark.

The broad tool surface creates security and privacy risks: arbitrary search/retrieval, sandbox execution, media uploads, credential handling, and trace retention. Repository text and remote content were treated as untrusted. V2 narrows network access, rejects private-address retrieval, validates redirects, limits writes, prohibits external side effects and authenticated browsing, and scores only final public-evidence artifacts.

## Original Benchmark Starting Point

The read-only original benchmark created an Evidence-Backed Research Report Agent with `report.md`, `sources.json`, and `run_report.json`. It had three public and eight hidden cases, most using synthetic closed corpora. Its two live-web cases required some official page inspection and access-depth disclosure. Its shared rubric emphasized report accuracy, synthesis, citations, uncertainty, and readability.

That version established a useful CLI and source-manifest pattern but did not make live deep browsing universal, did not require passage-level evidence, did not represent contradiction or source dependence in a graph, and used a 180-second/2 GiB envelope. Search snippets could still leave large portions of a case superficially answered.

## Evaluable Capabilities Retained and Increased

- One Markdown request and one uniform CLI.
- Iterative web search, selected-source retrieval, relevant-link traversal, and optional local browser rendering.
- Multi-source synthesis, conflict handling, source-quality judgment, calculations, and calibrated decisions.
- Human-facing Markdown plus machine-parseable audit artifacts.
- Honest access-depth and provider-use reporting.

V2 adds mandatory full-body/PDF evidence in every case, multi-hop entity and temporal resolution, source-lineage relations, exact claim-to-passage links, explicit contradiction edges, source-backed calculations with uncertainty, recovery from degraded sources, more independent evaluator research, a 600-second/4 GiB envelope, and `evidence_graph.json`.

## Excluded Portions

- MiroFlow's Hydra hierarchy, prompts, agent classes, MCP servers, delegation design, trajectory schema, benchmark loaders, and boxed-answer logic.
- Scoring architecture, code style, provider selection, tool-call sequence, and private chain-of-thought.
- Unrestricted APIs, authenticated websites, archive-bypass services, remote browser services, arbitrary code sandboxes, audio/video tasks, and future-event prediction.
- Any requirement to reproduce the reference repository or original benchmark's synthetic cases.

## Difference Between Reference and V2 Task

The V2 task is a create-agent benchmark for a **Deep Evidence Research Agent**, not a MiroFlow reconstruction. It makes a four-file final artifact the sole assessment surface and requires a report citation to resolve through a claim to exact inspected passages, contradictions, provenance relationships, and calculations. Live public evidence is mandatory in all eight cases. The task measures final evidence quality and decision utility, while permitting any compliant architecture and any conclusion supported by the artifacts.

## Source URLs Recorded in the Starting Analysis

The original analysis reviewed the repository, GitHub metadata, pinned raw files, official documentation, public product material, and a related arXiv record. Representative pinned URLs include:

- `https://github.com/MiroMindAI/miroflow`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/README.md`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/LICENSE`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/pyproject.toml`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/main.py`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/src/core/pipeline.py`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/src/core/orchestrator.py`
- `https://raw.githubusercontent.com/MiroMindAI/MiroFlow/fca7b4a7fda30e5a17b5c7b73d780738a925869f/docs/mkdocs/docs/tool_searching.md`
- `https://arxiv.org/abs/2511.11793v3`

No V2 case depends on these URLs or reveals the source repository to the agent builder.
