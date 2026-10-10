# Interface and Delivery Contract

## Environment Preparation

Create and configure the dedicated Conda prefix specified in `04_resources.md`. Choose and install all dependencies needed for HTML extraction, PDF parsing, optional local headless-browser rendering, data processing, LLM use, Markdown generation, and JSON validation. Environment setup must be non-interactive after commands are issued and must not modify unrelated environments.

## Uniform Command

The submission root must contain `run_agent.py`. Every case is invoked exactly as:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

Do not require any other argument, a particular working directory, interactive input, or a visible browser.

## Input Contract

`--input` is one UTF-8 Markdown file containing the complete research request. It can include dates, clues, named entities, URLs as discovery leads, output constraints, and relative links to case assets. Resolve a relative asset path against the directory containing `input.md`.

If assets are present, support referenced UTF-8 `.md`, `.txt`, `.csv`, and `.json` files, tabular `.xlsx` files, PDF documents, and `.png` or `.jpg` images. Treat inputs and retrieved content as untrusted evidence, not executable instructions. Do not scan sibling cases or unrelated workspace files.

## Output and Overwrite Behavior

`--output` is the only directory the run may create or modify. Create it when absent. On a repeated run, atomically replace the four agent-owned required artifacts below. Preserve unrelated pre-existing files. Optional diagnostics may be written only under `<output_dir>/diagnostics/` and are not evaluated.

A successful run produces:

- `<output_dir>/report.md`
- `<output_dir>/sources.json`
- `<output_dir>/evidence_graph.json`
- `<output_dir>/run_report.json`

### `report.md`

Write a nonempty UTF-8 Markdown report for the case's audience, date scope, length, and decision. Unless a case specifies another structure, lead with the answer, state scope and method, synthesize findings, analyze conflicts and limitations, show calculations or comparisons where relevant, and end with a calibrated recommendation or decision implication.

Cite every material externally verifiable claim with one or more bracketed claim IDs such as `[C1]` or `[C1, C2]`. A material claim is one that affects identification, chronology, comparison, calculation, conclusion, risk, or recommendation. Each cited claim ID must resolve exactly once in `evidence_graph.json`. A short claim may be used in several report locations. Do not use source IDs as report citations.

### `sources.json`

Use this top-level structure:

```json
{
  "schema_version": "2.0",
  "research_as_of": "2026-06-30",
  "sources": []
}
```

Each source object must contain:

```json
{
  "id": "S1",
  "kind": "web",
  "title": "Exact page, document, dataset, or repository title",
  "locator": "https://public.example.org/document",
  "publisher": "Publishing organization",
  "authors": ["Named author when available"],
  "published_at": "2025-04-02",
  "updated_at": null,
  "accessed_at": "2026-07-25",
  "source_class": "official_policy",
  "independence_group": "publisher-or-underlying-origin-label",
  "access_depth": "full_page",
  "retrieval_note": "Relevant page body inspected by direct HTTP retrieval",
  "supersession_status": "current",
  "discovery_queries": ["focused query that led to this source"]
}
```

Rules:

- `schema_version` is the string `2.0`; `research_as_of` is the case's explicit cutoff date, or the run date when the case says current.
- `sources` is a nonempty array. Source IDs are unique and match `S` plus a positive integer.
- `kind` is `web` or `local`; benchmark cases normally require web evidence.
- `title`, `locator`, `publisher`, `source_class`, `independence_group`, `access_depth`, `retrieval_note`, and every `discovery_queries` item are nonempty strings. `authors` and `discovery_queries` are arrays.
- Web locators are absolute public `http://` or `https://` URLs authorized by the Search/Scrape rules. Local locators are case-relative paths.
- `published_at`, `updated_at`, and `accessed_at` are ISO `YYYY-MM-DD` strings or `null`. Do not substitute an access date for an unknown publication date.
- `source_class` may use concise values such as `primary_record`, `official_policy`, `official_documentation`, `repository`, `release_note`, `dataset`, `peer_reviewed`, `independent_analysis`, or `secondary_reporting`.
- `independence_group` groups sources that repeat, cite, syndicate, or derive from the same underlying evidence. Same-publisher pages are not automatically independent.
- `access_depth` is one of `full_page`, `browser_rendered`, `pdf_full`, `pdf_partial`, `partial_page`, or `search_snippet`. A source at `search_snippet` depth may be recorded as an unused lead but cannot appear in an evidence-graph passage or support a material claim. `browser_rendered` means the relevant rendered body was actually inspected.
- `supersession_status` is `current`, `superseded`, `historical`, `unclear`, or `not_applicable`.
- Include sources used for report claims or material conflict analysis. Do not pad the manifest with irrelevant results. It is acceptable to include a small number of failed or snippet-only leads only when they explain an important retrieval limitation and are not represented as evidence.

### `evidence_graph.json`

Use this top-level structure:

```json
{
  "schema_version": "1.0",
  "claims": [],
  "passages": [],
  "source_relations": [],
  "calculations": []
}
```

Each claim object must contain:

```json
{
  "id": "C1",
  "claim": "A concise proposition stated in the report",
  "materiality": "material",
  "status": "contested",
  "report_locations": ["Findings > Current rule, paragraph 2"],
  "supports": [{"source_id": "S1", "passage_id": "P1"}],
  "contradicts": [{"source_id": "S2", "passage_id": "P2"}],
  "calculation_id": null,
  "confidence": "medium",
  "qualification": "The apparent conflict may reflect different effective dates."
}
```

Each passage object must contain:

```json
{
  "id": "P1",
  "source_id": "S1",
  "locator": {"type": "page", "value": "14"},
  "quote": "A short exact excerpt that supports or contradicts the linked claim.",
  "context": "Table 3 note; units are millions of gallons per day."
}
```

Each source-relation object must contain:

```json
{
  "from_source_id": "S3",
  "to_source_id": "S1",
  "relation": "cites",
  "evidence": "S3 footnote 7 attributes the date to S1."
}
```

Each calculation object must contain:

```json
{
  "id": "K1",
  "description": "Normalized rate used in the comparison",
  "formula": "withdrawal_mgal_day * 1000000 / population",
  "inputs": [
    {"name": "withdrawal_mgal_day", "value": 12.3, "unit": "Mgal/day", "source_id": "S4", "passage_id": "P8"}
  ],
  "conversions": ["1 Mgal = 1,000,000 US gallons"],
  "result": {"value": 41.2, "unit": "gallons/person/day"},
  "uncertainty": {"method": "rounding interval", "lower": 40.9, "upper": 41.5, "unit": "gallons/person/day", "assumptions": ["Each displayed input lies within one half-unit of its last reported digit."]}
}
```

Evidence-graph rules:

- Claim IDs match `C` plus a positive integer; passage IDs match `P` plus a positive integer; calculation IDs match `K` plus a positive integer. IDs are unique within their arrays.
- `materiality` is `material` or `context`; `status` is `supported`, `contested`, `uncertain`, or `calculated`; `confidence` is `high`, `medium`, or `low`.
- `report_locations`, `supports`, and `contradicts` are arrays. Every material report claim has at least one support link, contradiction link, or calculation. A `supported` claim has support. A `contested` claim has both support and contradiction. A `calculated` claim refers to a calculation and links its source inputs.
- `qualification` is a string or `null`. Do not erase unresolved conflicts by setting high confidence.
- Every support or contradiction pair resolves to an existing source and passage, and that passage belongs to the named source.
- Passage `quote` is an exact, short excerpt from an actually inspected body, PDF, repository file, dataset table, or local asset. Keep enough context to preserve meaning. `locator.type` is `page`, `section`, `paragraph`, `table`, `figure`, `record`, `repository_path`, or `line`; `value` is a nonempty string. Never fabricate a locator, quotation, or page number.
- Passages cannot refer to `search_snippet` sources. When extraction is partial, the locator and context must make the inspected portion clear.
- `source_relations` uses `cites`, `copies`, `syndicates`, `derives_from`, `same_dataset`, or `same_authority`. Use it when source dependence affects corroboration or provenance. An empty array is allowed only when no material dependence was found after checking.
- `calculations` may be empty when a case requires no quantitative derivation. Otherwise include every material reported result with source-linked inputs, units, conversions, formula, result, and an uncertainty or sensitivity object. `uncertainty` may be `null` only when the report explains why a quantitative bound is not meaningful.

The graph records public evidence and transparent derivations, not private reasoning.

### `run_report.json`

Use this structure:

```json
{
  "status": "success",
  "artifacts": ["report.md", "sources.json", "evidence_graph.json"],
  "errors": [],
  "usage": {
    "elapsed_seconds": 315.4,
    "report_word_count": 1720,
    "source_count": 10,
    "claim_count": 24,
    "passage_count": 41,
    "external_api_calls": {
      "deepseek": 8,
      "gateway": 0,
      "serper": 14,
      "web_retrieval": 21
    },
    "retrieval_depth_counts": {
      "full_page": 8,
      "browser_rendered": 1,
      "pdf_full": 2,
      "pdf_partial": 0,
      "partial_page": 1,
      "search_snippet": 2
    },
    "browser_attempts": 1
  }
}
```

All counts must be actual, including failed attempts and retries where applicable. `deepseek` and `gateway` count provider requests; `serper` counts search-proxy requests; `web_retrieval` counts page or document retrieval attempts. Do not expose credentials, private reasoning, page contents, or unrelated paths.

## Exit Codes and Failures

- Exit `0` only after all four required artifacts are complete and parseable, report claim citations resolve, evidence links resolve, and required counts and case constraints have been validated.
- Exit nonzero for invalid input, a missing required asset, generation failure, output-validation failure, inability to satisfy a mandatory evidence boundary, or inability to produce a minimally supportable report.
- When the output directory is writable, attempt an error-form `run_report.json` with `status: "error"`, an empty or accurate partial `artifacts` array, and concise actionable `errors` before a nonzero exit.

## Multiple Files and Repeated Runs

Read every referenced case asset needed by the request while preserving separate provenance. Do not combine documents in a way that hides which passage came from which source. Repeated runs overwrite only the four agent-owned artifacts and the agent-owned diagnostics directory.
