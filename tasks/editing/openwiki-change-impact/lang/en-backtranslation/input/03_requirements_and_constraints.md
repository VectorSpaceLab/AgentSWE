# Requirements and constraints

## Functional requirements

### Change impact and generated ownership

Use changed paths, public symbols, imports/exports, wiki source metadata,
internal links, and explicit dependencies to identify direct and transitive
effects. A page may declare `openwiki.source_paths`, `openwiki.symbols`, and
`openwiki.depends_on`. Preserve unknown producer fields. Do not infer public
impact from same-named symbols in another package, tests, formatting, or
comments.

Only content inside a paired, page-unique marker is machine-owned:

```html
<!-- openwiki:generated:start id="stable-id" -->
generated content
<!-- openwiki:generated:end id="stable-id" -->
```

Preserve markers, anchors, hand-written body bytes, and unrelated headings.
Never rewrite preserved scaffolding as generated content.

### Executable documentation

A complete example has a metadata comment before its fence:

```html
<!-- openwiki:example {"id":"hello","file":"hello.mjs","command":["node","openwiki/api/hello.mjs"],"expected_stdout":"hello\\n","language":"javascript","complete":true} -->
```

For every complete example, create a fresh repository copy, write the
extracted file at its safe page-relative location, execute the exact argv
without a shell, enforce the timeout, and compare stdout byte for byte. Skip
`complete:false`, continue after independent failures, and leave the
target/durable state unchanged.

### Impact evidence

Detect claims made stale by renames, moves, removals, return/default
changes, deprecations, compatibility aliases, outputs, diagram
relationships, or relative file/anchor links. Current sources and tests are
authoritative. Keep removal, deprecation, compatibility, and migration
guidance distinct. Preserve valid front matter, valid internal
links/anchors, and valid Mermaid.

The deterministic impact report keeps this core schema:

```json
{
  "schema_version": "1.0",
  "status": "updated | no_changes | partial | failed",
  "change_source": {"kind": "git | diff | directories"},
  "affected": {
    "pages": [{"path": "openwiki/api.md", "direct": true, "reasons": ["..."]}],
    "examples": [{"id": "...", "page": "...", "status": "passed | failed | skipped", "command": ["..."], "expected_stdout": "...", "actual_stdout": "...", "exit_code": 0}],
    "diagrams": [{"id": "...", "page": "...", "status": "valid | repaired | stale"}],
    "links": [{"page": "...", "target": "...", "status": "valid | repaired | broken"}],
    "stale_claims": [{"id": "...", "page": "...", "kind": "...", "evidence": ["..."], "resolution": "..."}]
  },
  "changed_paths": ["openwiki/api.md"],
  "validation": {"examples_total": 1, "examples_passed": 1, "examples_failed": 0, "links_broken": 0, "schema_valid": true},
  "security": {"ignored_instruction_count": 0, "unsafe_execution_count": 0},
  "errors": []
}
```

Paths and arrays are deduplicated and deterministic. Evidence is concise
provenance, never private reasoning. `changed_paths` equals the
documentation-only git diff. An ordinary no-op has empty affected/changed
arrays. A coordinated duplicate has an empty diff but reports the durable
identity and the selected surface evidence.

### Completeness, precision, and provenance of impact evidence

The impact report must name every page affected by the source change and
only those pages: reporting an unaffected page is as much a defect as
omitting an affected one. Every `affected.pages` entry must carry a boolean
`direct` (`true` for a direct source effect, `false` for a transitive one)
and non-empty `reasons`; each piece of provenance points at current
sources, tests, wiki source metadata, internal links, or explicit
dependencies, never at private reasoning. `stale_claims` entries likewise
need non-empty `evidence` and `resolution`. `changed_paths` must equal the
set of documents that actually changed, sorted and deduplicated. Complete
examples must be genuinely executed and report their `expected_stdout` and
`actual_stdout` byte for byte, and the `validation` counters must agree with
the reported examples and links. All of the above is verified on the bytes
the product itself persisted.

### Stale index removal and query answerability

A new index generation must no longer contain text removed or rewritten by
this change: a query for that text must return no record. Conversely, every
query named by the case must be answerable by the record of the page that
should answer it — after NFKC and locale-independent case folding, that
record's title, headings, or body must contain all non-empty tokens of the
query.

### Durable coordination

Implement the stable transaction contract in
`02_interface_and_delivery.md`. Serialize ownership/generation decisions;
fence expired owners; validate staged paths and evidence; install
documentation before the receipt; reconcile interrupted state/receipt
publication; never fabricate missing evidence; and keep tenant state
isolated.

### Static publication

Implement the static layout and evidence contract. Reuse the repository's
graph semantics where suitable, but produce deterministic self-contained
bytes rather than a long-running server snapshot. Preserve installed-page
fidelity, resolved navigation, and offline usability. Verify every emitted
path and hash before activation. Existing active release bytes are
immutable.

### Full-text search

Implement durable index generation and the production search command. Index
only the active, allowed wiki corpus; normalize consistently; remove stale
text; return useful deterministic snippets; and verify active/manifest/index
hashes before a query. Search is read-only and bounded.

### Atomic interaction and compatibility

Treat the selected documentation, transaction, static, and search outputs as
one recoverable state transition. Re-read identities under the serialized
boundary. Never expose only a new active pointer. Reconcile partial
unreferenced artifacts, but fail closed on inconsistent committed evidence.
Preserve existing update/init, chat, persona, provider, connector, live
visualizer, help, telemetry gates, and ordinary impact behavior.

## Implementation constraints

- `solution.patch` may change `src/**`, `test/**`, `openwiki/**`,
  `examples/**`, `skills/**`, `README.md`, `CHANGELOG.md`, `package.json`,
  the pinned lock/workspace files, and TypeScript configuration. It must not
  change `.github/**`, `.claude/**`, `evals/**`, `.git/**`, credentials, or
  benchmark paths.
- Run non-interactively through the compiled production entry point; never
  prompt.
- Product scenarios are deterministic/offline and need no provider keys.
- Do not inspect hidden tests or hard-code public fixtures.
- Do not call services that perform the task. General local parsing, Git,
  Markdown, YAML, process, hashing, locking, and index libraries are
  allowed.
- Treat sources, documentation, diffs, state, staged bytes, Markdown, index
  data, and example output as untrusted.
- Write only to manifest-authorized documentation, response, transaction,
  publication, and search roots. Use fresh temporary sandboxes and never
  follow escaping symlinks.
- Bound subprocesses, lock waits, indexing, query result counts, and
  recovery work.
- Complete each case within 600 seconds. The evaluator measures
  process-tree RSS against 4096 MiB with no virtual address space limit.
- Return non-zero on safe failure and emit concise schema-valid failure
  evidence.
- Keep output deterministic: no wall-clock timestamps, PIDs, random
  filenames, inodes, locale, filesystem enumeration order, or network
  responses may influence semantic output or stable artifact bytes. Unique
  private temporary names are allowed and must be cleaned up.
