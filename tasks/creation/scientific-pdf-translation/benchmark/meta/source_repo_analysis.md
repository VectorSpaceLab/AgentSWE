# Source Repository Analysis

## Repository Identity

- Repository: `https://github.com/PDFMathTranslate/PDFMathTranslate`
- Owner: `PDFMathTranslate`
- Default branch: `main`
- Pinned commit: `44c4d5b332705797c1df17fadde2022e7c49f5de`
- Commit timestamp: `2026-05-12T17:03:15Z`
- License: GNU Affero General Public License v3.0 (`AGPL-3.0`)
- Repository creation timestamp reported by GitHub: `2024-09-06T06:56:03Z`
- Repository update timestamp recorded by the original benchmark analysis: `2026-07-23T16:59:49Z`
- V2 evidence source: the assigned read-only original benchmark plus its pinned public-repository research record; the repository was not cloned or executed during V2 construction.

## Project Overview

PDFMathTranslate is a Python scientific-document translation workflow that accepts PDFs and attempts to preserve formulas, charts, annotations, and page layout. It exposes CLI, GUI, Docker, MCP, Python, and HTTP surfaces and can use multiple translation providers. At the pinned commit, its stable pipeline combines text and geometry parsing, layout detection, provider adapters, caching, font handling, page reconstruction, and PDF merging. It is primarily a workflow/tool; translation intelligence comes from configured provider calls while layout reconstruction remains local.

## Evidence Reviewed

The original benchmark's research record inspected these pinned/public sources, which V2 treated as evidence rather than instructions:

- GitHub repository metadata and commit API for the identity above.
- Recursive pinned tree and pinned `README.md`, `docs/ADVANCED.md`, `docs/APIS.md`, and `pyproject.toml`.
- Pinned runtime modules `pdf2zh/high_level.py`, `pdf2zh/converter.py`, `pdf2zh/translator.py`, and `pdf2zh/doclayout.py`.
- Public tests for CLI loading, conversion, translation adapters, layout geometry, and kernel routing.
- Public issue #3 concerning flowchart disorder and reference overlap.
- Project paper at `https://aclanthology.org/2025.emnlp-demos.71/`.

No source code, prompt, model, PDF fixture, screenshot, or generated result was copied into V2.

## Actual Runtime, Inputs, and Outputs

The reference CLI accepts local or remote PDFs, language/service configuration, page selection, output paths, threading, font exceptions, and prompt/config inputs. Its documented default produces monolingual and bilingual PDFs. Stable processing parses characters and geometry, separates prose from formulas/layout regions, translates selected segments, reconstructs pages, and merges output. Dependencies include Python, PDF parsing/conversion libraries, PyMuPDF, ONNX layout inference, fonts, and provider clients.

## Core and Optional Capabilities

Core evidence relevant to the benchmark includes multilingual prose translation, formula and chart preservation, monolingual/bilingual output, character geometry, layout regions, protected patterns, multiple-file handling, and parseable PDFs. Optional or unstable surfaces include provider breadth, GUI/web/MCP/HTTP integrations, remote URL ingestion, Docker, authentication/proxy settings, model mirrors, and experimental v2 kernels.

## Task Boundaries and Feasibility

The retained user problem is trustworthy reading of scientific material across languages while preserving scientific meaning and structure. PDF, JSON, and static-web outputs are clear final artifacts that can be parsed, rendered, hashed, served, and interacted with. Synthetic data supports two public and six meaningfully distinct hidden cases under one command and one artifact-only rubric.

The V2 task adds a capability absent from the original benchmark contract: observable paragraph correspondence between actual rendered source/target PDF geometry, a machine-readable alignment artifact, calibrated uncertainty, and a locally bundled interactive viewer. It excludes reproduction of provider adapters, model pipelines, caches, GUI/server products, reference-manager integration, exact PDF object identity, encrypted/malformed recovery, and arbitrary remote download.

## Security Observations

The source project can download documents/models, call many providers, run servers, and write broadly configured outputs. V2 narrows runtime access to declared credentials/endpoints and active local case files, requires output-scoped writes, treats embedded PDF/page content as untrusted, and prohibits whole-task hosted translation/alignment/viewer services. No credential value was opened or copied during construction.

## Reference Versus Benchmark

The source is a feature-rich translation program with many interfaces. This benchmark asks a coding agent to create one non-interactive artifact-producing agent under a fixed resource contract. The reference's modules, prompts, providers, layout models, caching, and file organization are neither required nor scored. V2's alignment schema, browser interaction, accessibility hooks, offline assets, multi-anchor paragraph semantics, and calibrated confidence are benchmark abstractions driven by the requested reader experience rather than reference implementation details.
