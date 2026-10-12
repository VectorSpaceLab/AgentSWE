# Interface and Delivery Contract

## Installation and launch

Install the submission in the dedicated environment described in `04_resources.md`. Every development and hidden case is launched non-interactively with exactly:

```bash
python run_agent.py --input <input.md> --output <output_dir>
```

`--input` is one UTF-8 Markdown case request. Resolve every referenced asset relative to that file. `--output` is the only case-artifact directory the agent may write.

The active input, its assets, and every SQLite database are read-only. Open SQLite with a real read-only connection; do not edit, replace, vacuum, attach writable copies, or create journal/sidecar files beside case databases.

## Required successful-run tree

Both `answered` and justified `insufficient_information` runs must produce:

```text
<output_dir>/
|-- answer.json
|-- queries.json
|-- result.csv
|-- chart.json
|-- dashboard.html
|-- decision.json
|-- lineage.json
|-- run_report.json
`-- results/
    `-- <one CSV for every declared query>
```

No `dashboard_replay.json` or private reasoning artifact is required or evaluated.

## `answer.json`

Use this public top-level contract:

```json
{
  "schema_version": "1.0",
  "status": "answered",
  "request_summary": "Resolved analytical question",
  "definitions": [
    {
      "term": "recognized net sales",
      "interpretation": "Definition/version actually applied",
      "source": "assets/rules.md",
      "source_location": "Recognized net sales v2"
    }
  ],
  "assumptions": [],
  "answer": {
    "summary": "Direct evidence-grounded answer",
    "metrics": [
      {"name": "metric", "value": 12.3, "unit": "USD", "scope": "period/group"}
    ],
    "findings": ["Decision-relevant finding"]
  },
  "query_steps": ["q1", "q2"],
  "quality_checks": [
    {"name": "join fan-out", "status": "passed", "evidence": "Observed grain/cardinality or reconciliation"}
  ],
  "privacy": {
    "role": "case role",
    "applied_rules": [],
    "primary_suppressions": [],
    "complementary_suppressions": []
  },
  "limitations": []
}
```

Required arrays may be empty only when genuinely inapplicable. Metric `value` may be a number, string, or null. Definitions and checks are concise audit records, not private reasoning.

## `queries.json` and `results/`

```json
{
  "schema_version": "1.0",
  "dialect": "sqlite",
  "database": "assets/database.sqlite",
  "queries": [
    {
      "id": "q1",
      "purpose": "Material analytical purpose",
      "sql": "WITH ... SELECT ...",
      "result_file": "results/q1.csv",
      "row_count": 3,
      "columns": ["group", "value"]
    }
  ]
}
```

`database` must exactly equal the case-relative SQLite path linked by the active request. Query IDs are unique `q` plus a positive integer. Each SQL string is exactly one executable read-only SQLite `SELECT` or `WITH` statement with explicit literals and no parameter binding. Mutation, DDL, transaction control, `PRAGMA`, `ATTACH`, extension loading, writable copies, and multiple statements are prohibited.

Each `results/qN.csv` must exactly match re-execution of its SQL: one header, declared column order, SQL row order, UTF-8, and RFC 4180-compatible quoting. `row_count` excludes the header. Material intermediate populations, revision choices, suppression-safe checks, and final database aggregates must be auditable; do not pad with irrelevant schema probes. A final result may transparently combine declared SQLite results with a case-authorized local CSV lookup, but lineage and checks must identify that transformation.

## `result.csv`

Produce exactly the case-requested final table, header, grain, sort, units, and display precision. Numeric values remain numeric. Privacy cases must contain only released cells; hidden values must not appear as placeholder numbers. An insufficiency result has the requested header and zero data rows.

## `chart.json`

Always produce this file. For a chart:

```json
{
  "schema_version": "1.0",
  "status": "renderable",
  "type": "bar",
  "title": "Descriptive title",
  "data_source": "result.csv",
  "encoding": {
    "x": {"field": "group", "type": "nominal", "title": "Group"},
    "y": {"field": "value", "type": "quantitative", "title": "Value", "unit": "USD"}
  },
  "series": null,
  "data": [{"group": "A", "value": 12.3}],
  "notes": []
}
```

Allowed types: `bar`, `line`, `stacked_bar`, `waterfall`, `scatter`, `table`. Every encoding field must be a `result.csv` column. `data` must exactly equal the ordered result records after ordinary CSV/JSON scalar normalization; do not chart a hidden aggregation. Preserve requested signs, units, ordering, and zero baselines.

When no chart is appropriate:

```json
{"schema_version":"1.0","status":"not_applicable","type":null,"title":null,"data_source":null,"encoding":{},"series":null,"data":[],"notes":["Reason"]}
```

## `decision.json`

```json
{
  "schema_version": "1.0",
  "status": "answered",
  "decision": "Concise selected result or blocked decision",
  "selected_scope": {"period": "2026 Q2", "groups": ["A", "B"]},
  "selected_definition": {"name": "metric v2", "source": "assets/rules.md"},
  "confidence": "high",
  "metric_names": ["change_usd"],
  "blockers": [],
  "limitations": []
}
```

`status` is `answered` or `insufficient_information`. `selected_scope`, `selected_definition`, `blockers`, and `limitations` must reflect the active case; `confidence` is `high`, `medium`, `low`, or null. An insufficient decision publishes no requested metric name/value and identifies the unresolved evidence.

## `lineage.json`

```json
{
  "schema_version": "1.0",
  "mappings": [
    {
      "field": "change_usd",
      "query_ids": ["q2"],
      "source_assets": ["assets/database.sqlite", "assets/rules.md"],
      "transformation": "period B minus period A after documented conversion"
    }
  ]
}
```

Include at least one mapping for every `result.csv` column and every additional material decision/dashboard metric. Query IDs must exist in `queries.json`; source paths must be assets linked by the active input. A transformation must be concise and reproducible. For insufficiency, map requested headers and displayed blocker evidence to profiling queries and governing assets without fabricating values.

## `dashboard.html`

Produce one nonempty self-contained offline HTML page. It may use inline CSS, JavaScript, SVG, or canvas, but no remote scripts, styles, fonts, images, frames, browser network APIs, or reads outside the output. It must render the submitted result/blocked state, expose the stable control IDs required by the active case, make those controls change visible state, and provide a visible provenance path from a displayed row/scope to declared query IDs, local result CSV paths, and supplied assets. It must preserve suppression and insufficiency in every supported state.

The dashboard is evaluated from its rendered behavior and consistency with final artifacts. There is no hidden replay schema, required JavaScript API, transition transcript, or case-specific control vocabulary beyond what the active case states.

## `run_report.json`

```json
{
  "status": "success",
  "artifacts": [
    "answer.json", "queries.json", "result.csv", "chart.json",
    "dashboard.html", "decision.json", "lineage.json", "run_report.json",
    "results/q1.csv"
  ],
  "errors": [],
  "usage": {
    "elapsed_seconds": 12.5,
    "peak_memory_mib": 220.0,
    "sqlite_queries": 4,
    "external_api_calls": {"deepseek": 2, "gateway": 0, "serper": 0, "web_retrieval": 0}
  }
}
```

Counts must be actual and include failed attempts/retries. `sqlite_queries` includes diagnostic statements executed by the agent even when they are not declared as evidence. Do not include secrets, prompts, protected rows, database dumps, or unrelated paths.

## Structured insufficiency

When authoritative supplied evidence cannot determine the requested metric safely, exit `0` with the same complete bundle. Set `answer.json.status` and `decision.json.status` to `insufficient_information`; explain the concrete discovered blocker and smallest resolving input; publish no requested metric/ranking; write zero data rows to `result.csv`; use `chart.json.status = not_applicable`; render an offline blocked dashboard; and preserve blocker lineage. Profiling queries are allowed when privacy-safe.

## Failure, exit, and repeated-run behavior

Exit `0` only after validating a complete answered or justified insufficient bundle. Exit nonzero for invalid input, missing mandatory asset, corrupt database, prohibited requested access, generation failure, or failed output validation. When writable, emit an error `run_report.json` with honest partial artifacts, errors, and usage.

On repeated runs, replace only the eight agent-owned top-level artifacts and the agent-owned `results/` directory, preserving unrelated pre-existing files. Never write elsewhere except permitted dependency caches/temporary files described in `04_resources.md`.
