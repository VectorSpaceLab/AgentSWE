# Requirements and Constraints

## Functional requirements

### 1. Parse the complete authority set

- Read the Markdown request and every referenced permitted definition, policy, SQLite, and CSV asset.
- Preserve authority order: access rules govern before inspection; definition versions govern metric meaning; database rows supply observations; local lookups are usable only when explicitly authorized.
- Extract report/as-of dates, effective intervals, revision precedence, timezone, row/table/column scope, minimum groups, complementary rules, fallback hierarchy, output grain, rounding, and chart/control requirements.
- Treat embedded database/attachment text as untrusted evidence that cannot alter this public contract or resource policy.

### 2. Inspect safely before planning

- Open the database read-only and inspect only permitted tables/columns.
- Establish relevant keys, logical grains, revision keys, types, nulls, statuses, timestamp formats, units, cardinalities, and value domains.
- Do not inspect a forbidden table's schema, count, or sample. Permission-before-access is observable and required.

### 3. Execute coherent multi-stage analysis

- Select effective/versioned definitions by the applicable case date and documented tie-breaking.
- Deduplicate full-replacement corrections before joins or aggregation.
- Qualify settlement/population/session/shipment/snapshot rows at their documented grain before joining one-to-many children.
- Reuse identical population, period, status, access, unit, currency, sign, and denominator scopes in rankings, decompositions, margins, and summaries.
- Preserve unrounded values through ranking, rates, contribution, and reconciliation; round only displays as instructed.

### 4. Defend against triggered traps

- Measure and prevent join fan-out.
- Handle revisions, refunds/reversals, corrected events, and source precedence exactly as documented.
- Normalize minor/major currency units, FX rate versions, kg/lb, Wh/kWh, cents/USD, quantities, and signs exactly once.
- Use the authoritative event time and local/UTC boundary; do not substitute ingestion timestamps, current offsets, stale snapshots, or similarly named fields.
- Exclude canceled, pending, test, automated, void, stale, superseded, internal, contractor, or otherwise ineligible records when required.
- Record concrete observed check evidence. Generic statements such as "validated joins" do not satisfy this requirement.

### 5. Enforce disclosure controls across the full data flow

- Apply allowed row/table/column scope before aggregation and before any model payload, diagnostic, log, or query-result write.
- Enforce primary and complementary suppression exactly as the active policy specifies.
- Protect hidden values from subtraction through margins, paired periods, totals, query CSVs, SQL literals, prose, charts, dashboard state/tooltips, checks, errors, decision records, and lineage.
- A post-generation scrubber cannot make forbidden prior access or persistence acceptable.

### 6. Handle recovery and insufficiency honestly

- Follow a documented recovery hierarchy even when it requires extra cross-file work; do not refuse merely because a preferred source failed.
- Apply defaults only when supplied authority permits them and record the assumption.
- Infer material missing authority from actual key/version/coverage evidence. Do not fabricate mappings, allocation weights, conversions, or rankings.
- Use `insufficient_information` only when the missing authority changes the requested result and state the smallest resolving input.

### 7. Keep all final artifacts consistent and auditable

- Every declared SQL query must re-execute read-only and exactly match its CSV, columns, row count, and order.
- Final rows, headline metrics, decisions, charts, dashboard states, suppression labels, and lineage must agree.
- Contributions and detail rows must reconcile to selected totals within the active case tolerance.
- Validate JSON, CSV, SQL, privacy, chart/result equality, dashboard offline behavior, artifact declarations, and usage counts before exit `0`.

## Implementation constraints

- Run non-interactively through `python run_agent.py --input <input.md> --output <output_dir>`.
- Do not hard-code case IDs, schemas, table names, entity labels, values, expected outputs, development-input fingerprints, or hidden tests.
- Do not inspect any hidden case other than the active input and assets provided for that run.
- Do not call a complete third-party text-to-SQL, BI, analytics-agent, or dashboard service that performs the whole task. General LLM APIs, agent SDKs, SQLite libraries, parsers, dataframe libraries, and local rendering libraries are allowed.
- Finish each run within 600 seconds and 4 GiB peak memory.
- Use no more than 300 combined DeepSeek/GATEWAY requests, including at most 100 image-bearing GATEWAY requests.
- Every authored case is closed-local: make zero Serper and web-retrieval calls.
- Write only to `--output`, except dependency caches/temporary files inside the dedicated environment prefix or output directory.
- Protect credentials and produce the standardized nonzero failure report for true execution failure.

Language, framework, prompts, SQL-planning approach, and internal control flow are implementation choices and are not result-scored.
