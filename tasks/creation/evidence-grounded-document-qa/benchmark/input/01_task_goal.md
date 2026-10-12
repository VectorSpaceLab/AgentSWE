# Task Goal

Build a task-specific evidence-grounded document QA agent for analysts who must make decisions from frozen local document packets. Your agent receives a complete natural-language request plus referenced files and produces a concise answer whose material claims can be traced to exact native source locations and replayed in an offline evidence viewer.

The core value is reliable synthesis across heterogeneous sources rather than search alone. The agent must join facts distributed across prose, tables, spreadsheets, formulas, PDF pages, SVG figures, and raster images; apply the request's entity and revision rules; perform multi-step calculations with units; retain conflicts; resist plausible distractors; and abstain at claim level when the packet does not support an answer.

The final objective is an auditable answer package containing:

- a readable `answer.md`;
- structured atomic claims and evidence in `claims_and_citations.json`;
- a self-contained offline `review.html` with reciprocal claim/source navigation;
- `review_manifest.json` linking every claim and evidence ID to exact native locations;
- `source_bundle.json` preserving exact cited-source bytes and SHA-256 hashes; and
- a minimal `run_report.json`.

Non-goals: do not modify source documents, search the web for closed-corpus cases, invent missing facts, convert every source into untraceable plain text, expose chain-of-thought, imitate a particular repository, or require a particular agent SDK, prompt structure, vector database, OCR engine, or framework.
