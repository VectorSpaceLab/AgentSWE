# Interface and delivery

## Repository installation

From the supplied OpenWiki root, use Node.js 22 or later and the package
manager pinned in `package.json`:

```bash
pnpm install --frozen-lockfile --ignore-scripts
```

Do not change dependency versions merely to install fixtures. The evaluator
compiles both TypeScript projects and runs the focused upstream regressions
after applying the patch.

## Builder invocation

The controller launches the coding agent non-interactively:

```bash
python run_agent.py \
  --input <benchmark-input-directory> \
  --repository <writable-pinned-repository> \
  --dev-cases <public-dev-cases-directory> \
  --output <delivery-directory>
```

The four input documents, the repository, the public cases, and the output
directory are independent paths. Exit `0` only after a valid delivery has
been produced. On failure exit non-zero and, where possible, write a
schema-valid `run_report.json`. Repeated builder runs overwrite only the
three required delivery files.

## Impact update command

Extend the compiled production CLI:

```bash
node <openwiki-source>/dist/cli.js --update \
  --impact-manifest <manifest.json> --print
```

The working directory is the repository being maintained. Impact mode must
bypass credentials/models, make no network calls, update the
documentation, write the selected response artifacts, and exit. Success and
a genuine no-op use `0`. Invalid or unsafe input, stale/conflicting
identities, incomplete commit evidence, example failures, or artifact
publication failures are non-zero with concise diagnostics and, when safe,
a schema-valid failure report.

Every manifest keeps the Cycle 001 fields:

```json
{
  "schema_version": "1.0",
  "change_source": {"kind": "git", "base": "HEAD~1", "head": "HEAD"},
  "docs_root": "openwiki",
  "report_path": "impact-report.json",
  "diff_path": "documentation.diff",
  "example_timeout_ms": 5000
}
```

Support `git`, `diff`, and `directories` change sources. Git revisions and
manifest paths are repository-local. Directory roots may be absolute or
repository-relative, are read-only, and must be distinct. Reject traversal,
malformed diffs, missing roots/revisions, symlink escapes, and overlapping
outputs. Ignore benign unknown manifest fields.

## Durable transactions

The controller may add the existing coordination object:
```json
{
  "transaction": {
    "state_dir": ".openwiki-impact",
    "tenant_id": "docs-team-a",
    "request_id": "update-017",
    "generation": 17,
    "payload_digest": "9f4b...exactly 64 lowercase hexadecimal characters...",
    "owner_id": "worker-2",
    "lease_ms": 30000
  }
}
```

IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. `generation` is a non-negative
safe integer, `lease_ms` is `1000..300000`, and the controller-supplied
digest identifies the logical payload. `state_dir` is repository-relative
and may not overlap the documentation or the response/publication/search
paths.

The stable paths remain:

```text
<state_dir>/tenants/<tenant_id>/state.json
<state_dir>/tenants/<tenant_id>/claims/<request_id>.json
<state_dir>/tenants/<tenant_id>/receipts/<request_id>.json
<state_dir>/tenants/<tenant_id>/staging/<request_id>/...
<state_dir>/tenants/<tenant_id>/write.lock
```

Use unique same-directory temporary files, flush file content before the
atomic rename, and serialize the complete read/check/write decision. Claims
record the schema, identity, generation, digest, owner, stage (`claimed` or
`prepared`), and an ISO-8601 `lease_expires_at`. Only expired foreign leases
may be recovered. Live foreign ownership is a bounded conflict.

`state.json` records the schema, tenant, latest generation, and latest
request. A committed receipt is immutable UTF-8 JSON with the transaction
identity, `status: "committed"`, sorted changed paths, installed
documentation hashes, the raw result counters, and the publication/search
evidence described below when selected. The impact report includes the
matching transaction identity, the receipt path, and the result
`committed`, `duplicate`, or `recovered`; safe failure responses may use
`conflict`, `stale`, or `incomplete`.

An exact same-identity retry is receipt-backed and performs no edits. A
digest mismatch, an older generation, or a different request at the latest
generation is refused. Tenants are independent. Reconcile processed expired
claims, partial staging, missing responses, or interrupted state/receipt
publication. Never acknowledge a receipt whose installed evidence is
missing, corrupt, or mismatched.

Without `transaction`, keep the ordinary one-shot/replay behavior and
neither read nor modify transaction state.

## Static publication surface

Publication is independently optional. The manifest selects it with:

```json
{
  "publication": {
    "root": ".openwiki-public",
    "site_id": "api-handbook",
    "generation": 17,
    "base_path": "/handbook/"
  }
}
```

`root` is repository-relative, written only by this feature, distinct from
all other roots, and symlink-safe. `site_id` follows the ID rules.
`generation` is a non-negative safe integer and must equal the selected
transaction generation. A normalized `base_path` starts and ends with `/`,
contains no `..`, query, fragment, or URL scheme, and is used only for local
navigation.

The stable publication layout is:

```text
<root>/sites/<site_id>/active.json
<root>/sites/<site_id>/releases/<generation>/manifest.json
<root>/sites/<site_id>/releases/<generation>/index.html
<root>/sites/<site_id>/releases/<generation>/graph.json
<root>/sites/<site_id>/releases/<generation>/assets/...
<root>/sites/<site_id>/releases/<generation>/pages/...
```

`active.json` contains the schema, site ID, generation, payload digest,
release path, and manifest SHA-256. `manifest.json` repeats that identity,
records the normalized base path, the included documentation paths sorted
with their installed SHA-256 values, and a sorted SHA-256 map of every other
release file. Release bytes are immutable. Use the stable field names
`documentation_hashes` and `files` for those two maps. Render every included
`openwiki/x.md` as `pages/x.html`, and include `index.html` and `graph.json`
in the release hash map (the manifest hashes every release file except
itself).

The static result must be a useful browsable rendering of the active wiki
graph: titles, types, descriptions/tags, page bodies, resolved
edges/backlinks, and working page navigation reflecting the installed
documentation. Exclude `INSTRUCTIONS.md`, `_plan.md`, `log.md`, non-Markdown
files, and symlink escapes. All runtime assets live inside the release; no
CDN, remote font/script/image, live API, SSE, filesystem URL, or absolute
host dependency is allowed. Treat Markdown/HTML as untrusted: scripts,
event handlers, `javascript:` URLs, unsafe raw HTML, and terminal control
payloads must not become executable page content.

Add top-level `publication` evidence to the impact report with the schema,
site ID, generation, digest, active/manifest paths and hashes, the page
count, and the status `published`, `unchanged`, or `failed`.

## Full-text search interface

The search index is independently optional:

```json
{
  "search": {
    "root": ".openwiki-search",
    "index_id": "api-handbook",
    "generation": 17
  }
}
```

The root and ID follow the publication path rules. The selected generation
must equal the transaction generation. The stable storage is:

```text
<root>/indexes/<index_id>/active.json
<root>/indexes/<index_id>/generations/<generation>/manifest.json
<root>/indexes/<index_id>/generations/<generation>/index.json
```

Active/manifest identity and hash evidence mirror publication.
`index.json` is deterministic UTF-8 JSON with one record per included wiki
page containing the path, title, headings/anchors, searchable plain text,
the installed documentation hash, and enough positional information to
return useful snippets. Do not index preserved scaffolding, markup syntax
as terms, example metadata, generated artifact roots, or symlink escapes. A
new generation removes stale terms from changed/deleted documents. Use an
object with `schema_version`, `index_id`, `generation`, `payload_digest`,
and sorted `documents`; each record uses `path`, `title`, `headings`,
`text`, and `documentation_sha256`. The generation manifest uses
`documentation_hashes` and `index_sha256`, and `active.json` names its
`generation_path` and `manifest_sha256`.

Expose search through the compiled production CLI:

```bash
node <openwiki-source>/dist/cli.js search \
  --index-root .openwiki-search \
  --index-id api-handbook \
  --query "exact user text" \
  --limit 10 --json
```

The search command writes one JSON object to stdout, not to repository
files. Its schema is:
```json
{
  "schema_version": "1.0",
  "index_id": "api-handbook",
  "generation": 17,
  "payload_digest": "...",
  "query": "exact user text",
  "results": [
    {"path": "openwiki/api.md", "title": "API", "anchor": "api", "snippet": "...", "score": 12}
  ]
}
```

Normalize query and index text with Unicode NFKC and locale-independent
case folding/lowercasing. All non-empty query tokens must match a result.
Rank title and heading matches before body-only matches, then break ties
deterministically by path/anchor. Scores are finite non-negative numbers.
Enforce `limit` in `1..100`. Invalid options, missing/corrupt active
evidence, hash mismatches, or an incomplete generation exit non-zero
without leaking index content.

Add top-level `search_index` evidence to the impact report with the schema,
index ID, generation, digest, active/manifest paths and hashes, the document
count, and the status `indexed`, `unchanged`, or `failed`.

## Cross-surface commit and recovery

When transaction, publication, and search are selected, they are one
recoverable logical commit. Stage and verify the documentation, examples,
diff/report evidence, the complete static release, and the complete search
generation before changing any active pointer or publishing the receipt.
After success all installed hashes and all generation/digest identities
agree.

Readers may observe either the previous complete active generation or the
new complete active generation, never a hybrid. On restart, reconcile
unreferenced partial releases and generations without trusting them. A
same-identity duplicate keeps documentation, receipt, release, index, and
active pointer bytes stable and rebuilds only the selected response files.
Conflicts, stale requests, unsafe artifacts, corrupt committed receipts, a
missing active generation, or mismatched publication/index evidence must
fail closed and preserve the last complete visible generation.

When selected individually, publication and search also work individually.
An ordinary impact manifest that selects neither keeps the Cycle 001 write
boundary unchanged. The existing live `openwiki visualize` command stays
compatible.

## Field naming for surface artifacts

`state.json`, committed receipts, the publication and index `active.json`
and `manifest.json`, `index.json`, and the `publication`, `search_index`,
and `transaction` evidence blocks in the impact report must all declare
their own schema: the field is named `schema_version` or `schema`, and the
value is any non-empty string. The evaluator does not lock a particular
schema literal; the `"1.0"` that appears in the JSON examples above
constrains only the files those examples belong to (the impact manifest,
the impact report, and the stdout of the `search` command). Identity fields
remain verbatim-equal: `site_id`, `index_id`, `tenant_id`, `generation`, and
`payload_digest` must exactly match the values given by the manifest and
the transaction. The latest-request field of `state.json` may be named
`latest_request` or `latest_request_id`; the result field of the impact
report's `transaction` block may be named `result` or `outcome`, with the
value still `committed`, `duplicate`, or `recovered`.

## Agent-loop result contract

When running under the agent loop, the evaluator launches the compiled
production entry non-interactively:

```bash
node <openwiki-source>/dist/cli.js --update --print --modelId <model> <case-request>
```

The product must write `agent_result.json` at the working directory root
and must emit the same completed result object verbatim as the last JSON
value on its stdout. Authorship is confirmed by the byte-for-byte equality
of that terminal JSON and the file; a delivery that differs, is written
elsewhere, or does not match the schema is a candidate artifact-contract
failure, not an evaluator fault. The object schema is
`schemas/agent_result.schema.json`:

```json
{
  "schema_version": "openwiki-agent-result/v1",
  "case_id": "<current case>",
  "observations": ["real tool/CLI trajectory and dynamic observations"],
  "integrity": {"receipt_path": "...", "documentation_sha256": {},
                "publication_manifest_sha256": "...", "search_index_sha256": "..."},
  "decision": {"completion_claim": "complete | partial | no_change | conflict | untrusted",
               "rationale": "..."}
}
```

The result must be produced by the product in this run through model calls
to the evaluator's broker: a result written without any model call has no
native authorship and scores 0 as a candidate failure (the evaluator judges
by the broker's call delta, not by the artifact content).

The model's answers must be consumed into the maintenance itself — used to
decide the affected set, documentation repairs, publication, or index
actions — not transcribed as delivered content. `observations` holds only
the product's own real tool/CLI trajectory and dynamic observations;
copying model text returned by the broker verbatim into `observations` or
`decision.rationale` (for example treating a model reply of "I need more
information" as an observation or rationale) is an artifact-contract
violation; the evaluator judges by the verbatim overlap between the reply
text recorded by the broker and the delivered artifact text, and names it
in the feedback. This clause does not change the per-case lower-agent turn
budget.

The result object must be emitted incrementally: write it out as soon as
the first product response arrives, and refresh it after every subsequent
step. When stopping early, set `decision.completion_claim` to `partial` and
list the parts not attempted; never claim steps that were not executed.
`integrity` may only cite digests that were genuinely generated and read.

## Builder delivery

Write exactly these three files into the delivery directory:

1. `solution.patch`: a non-empty UTF-8 unified Git patch relative to the
   supplied repository. It passes `git apply --check` on a pristine copy and
   does not apply twice.
2. `edit_report.json`: schema `1.0`, a non-empty `feature_summary`, the
   exact patch `changed_paths`, factual `commands` entries (`command`,
   `exit_code`, `result`), and string arrays `compatibility_notes` and
   `limitations`.
3. `run_report.json`: a schema `1.0` object with a non-empty string
   `status`; `artifact_paths` equal to
   `["solution.patch", "edit_report.json", "run_report.json"]`; a string
   array `errors`; a non-negative numeric top-level `runtime_seconds`; a
   non-negative integer top-level `peak_memory_bytes`; and `api_calls`
   containing non-negative integer `gateway`, `serper`, and `web_retrieval`
   counts.

Nested runtime objects, `peak_memory_mb`, `resource_usage`, or renamed call
counters are not substitutes. Do not include credentials, source dumps,
hidden guesses, private reasoning, fabricated command results, or extra
delivery files.
