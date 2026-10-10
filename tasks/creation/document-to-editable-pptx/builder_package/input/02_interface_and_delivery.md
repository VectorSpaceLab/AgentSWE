# Interface and Delivery Contract

## Environment Preparation

Create and configure the dedicated Conda prefix in `04_resources.md`. Install all dependencies non-interactively into that prefix and verify the agent with both public development cases. Do not modify unrelated environments or embed credentials in code or dependency files.

The harness activates the dedicated prefix, sets `PYTHONNOUSERSITE=1`, and loads the protected credential environment before invoking the agent. The submission must not require an installation step during an individual case run.

## Uniform Run Command

The submission root must contain `run_agent.py`. Every case is invoked as:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

Do not require any other arguments, environment-specific working directory, interactive input, or human confirmation.

## Input Contract

`--input` is the path to one UTF-8 Markdown file containing the complete presentation request. It may contain headings, prose, lists, Markdown tables, public URLs, and relative links to supporting files.

Resolve relative paths against the directory containing `input.md`. One request may reference multiple local files under a sibling `assets/` directory. Supported local inputs must include UTF-8 `.md`, `.txt`, `.csv`, and `.json`; `.pdf`; `.png` and `.jpg`; and `.svg`. Treat all supplied files as read-only.

Do not scan unrelated files. Reading is limited to the submission, the active case directory, the dedicated Conda prefix, normal system resources, and the protected credential file as needed.

## Output Contract

`--output` identifies the only directory the run may create or modify. Create it when absent. On a repeated run, atomically replace agent-owned `deck.pptx`, `source_manifest.json`, and `run_report.json`; do not delete unrelated files.

A successful run must produce all three artifacts:

- `<output_dir>/deck.pptx`: a valid OOXML PowerPoint presentation.
- `<output_dir>/source_manifest.json`: UTF-8 JSON mapping sources and material claims/calculations to slides.
- `<output_dir>/run_report.json`: UTF-8 JSON reporting status and actual resource use.

Optional diagnostics may be stored only under `<output_dir>/diagnostics/`. Evaluation does not depend on them.

## `source_manifest.json`

Use this minimum structure; additional non-sensitive fields are allowed:

```json
{
  "version": "1.0",
  "generated_at_utc": "2026-01-01T00:00:00Z",
  "research_scope": "live_web",
  "sources": [
    {
      "id": "S1",
      "title": "Example source",
      "publisher": "Example publisher",
      "url_or_path": "https://example.org/source",
      "published_date": "2025-12-01",
      "accessed_at_utc": "2026-01-01T00:00:00Z",
      "access_status": "used",
      "access_depth": "full_page",
      "retrieval_note": "Relevant body and table inspected.",
      "license_or_use_basis": "Public factual source; cited.",
      "cited_slides": [2, 3]
    }
  ],
  "claims": [
    {
      "claim_id": "C1",
      "slide": 2,
      "claim": "Example material claim",
      "source_ids": ["S1"]
    },
    {
      "claim_id": "C2",
      "slide": 3,
      "claim": "Example calculated result",
      "source_ids": ["S1"],
      "calculation": {
        "formula": "numerator / denominator",
        "inputs": {"numerator": 25, "denominator": 100},
        "result": 0.25,
        "displayed_as": "25.0%",
        "rounding": "one decimal place"
      }
    }
  ]
}
```

For a closed corpus, set `research_scope` to `closed_corpus`, record local files as `local_file`, and make zero search/retrieval calls. Record a blocked or unusable lead with `access_status: "lead_unavailable"`, the actual access depth, and a concise `retrieval_note`; do not cite it as support. Allowed access-depth values are `local_file`, `full_page`, `partial_page`, and `search_snippet`. Search snippets support only what is visible in the snippet.

The manifest must cover every source materially used by the deck, every externally grounded material claim, and every material calculation. It is an audit record, not private chain-of-thought.

## `run_report.json`

Use this structure:

```json
{
  "status": "success",
  "artifacts": ["deck.pptx", "source_manifest.json"],
  "errors": [],
  "usage": {
    "elapsed_seconds": 42.5,
    "peak_memory_mb": 768,
    "slide_count": 8,
    "external_api_calls": {
      "deepseek": 2,
      "gateway": 1,
      "gateway_image": 1,
      "serper": 3,
      "web_retrieval": 7
    }
  }
}
```

Counts must be actual integers, including zeros. `gateway_image` is a subset of `gateway`. Retries count. Do not infer, estimate, or fabricate provider counts. `artifacts` lists valid final artifacts and need not list the report itself.

On failure, use `status: "error"`, list only valid artifacts, and add concise actionable strings to `errors`. Do not include secrets, private reasoning, source-file contents, or stack traces containing unrelated paths.

## Exit Codes

- Exit `0` only after all three required artifacts are complete and parseable and the deck passes the agent's own preflight.
- Exit nonzero for invalid input, missing required assets, unrecoverable research failure, package-generation failure, or final validation failure.
- When the output directory is writable, attempt to write an error-form `run_report.json` before a nonzero exit.
