# Research Request: Offline PDF Extraction Stack Decision

Prepare a 2,000-2,600 word Markdown technical landscape report for a government digital-archives engineering team. The team must choose whether **GROBID**, **Docling**, or **Marker** should enter a six-week offline pilot for converting a mixed collection of born-digital and scanned technical PDFs into structured text and tables.

Evaluate public evidence available through **2026-06-30**. The deployment environment is Linux, CPU-only for the initial pilot, disconnected during processing, and limited to open-source components whose licenses permit internal modification. The archive needs page-linked text, reading order, table extraction, OCR for scanned pages, repeatable batch execution, and enough metadata to trace output to source pages. Do not assume that a feature mentioned in a README is production-ready or comparable across tools.

## Required Research

- Pin the current relevant release or repository state of each candidate as of the cutoff. Distinguish stable releases, pre-releases, default-branch documentation, and historical papers.
- Compare architecture, supported inputs/outputs, OCR path, table handling, page/coordinate provenance, CPU/GPU expectations, offline model or asset requirements, extensibility, license, maintenance signals, and documented limitations.
- Inspect official documentation, actual repository files, issue or release records, and project papers/PDFs. Trace a feature claim to code-facing or release evidence where feasible.
- Use at least two independent evaluations or comparative datasets. Examine task definitions, corpus composition, metrics, hardware, tool versions, and author/vendor dependence before using headline results. Do not rank incomparable benchmark numbers as if they came from one test.
- Identify at least four consequential conflicts, omissions, or version mismatches and state whether they are resolved.
- Recommend at most two pilot entrants. Define a 30-document test corpus, measurable pass conditions, resource measurements, and failure/rollback criteria tailored to the archive.

## Evidence Depth

Inspect at least **12 distinct source bodies** from at least **7 publishers or repository owners**. Include, for each candidate, official documentation plus a repository file or release/issue record; include at least three full PDFs or papers in total and two independent sources. At least nine sources must be body-level or full-PDF evidence. Search snippets are leads only.

## Report Format

Lead with the shortlist and confidence level. Include a version-pinned comparison matrix, an evidence-comparability table, conflict analysis, and the pilot protocol. Use `[C#]` citations. In `evidence_graph.json`, represent version-specific claims, contradictions, shared benchmark/data dependence, and any quantitative benchmark result with exact passages and calculation records where normalization is performed.
