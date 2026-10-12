# Hidden case 3: generations, conflicts, and tenant isolation

Maintain the library after `legacyFetch` was removed and `fetchRecord` was
deprecated in favor of `getRecord`. The first coordinated tenant request
must distinguish removal, deprecation, and migration and commit exact
executable evidence.

Next, an older generation for the same tenant arrives with a different
request. Reject it immediately without changing files or the tenant
receipt. A second tenant then submits its own first-generation request
against the already-correct repository; it must commit an independent
no-change receipt and must not inherit or alter the first tenant's
generation. Preserve the historical hand-written note, anchors, and the
whole unrelated create page.

The committed generation must have a complete offline publication and a
durable full-text index. Search must distinguish the migration page from
the unchanged create page, and the stale-generation rejection must leave
both the active pointers and the first tenant's receipt untouched.

Product contract (identical for every case): do all work through the
compiled production CLI; do not hand-edit documentation or forge state
files. The impact report must list only the genuinely affected pages and
give each a `direct` flag and non-empty `reasons`; `changed_paths` must
equal the set of documents that actually changed. The new index generation
must no longer return text removed by this change, while every query named
by the case must be answerable by the page that should answer it. Write
`agent_result.json` as soon as the first product response arrives and
refresh it after every step, emitting the same object verbatim as the last
JSON value on stdout; when stopping early, use `partial` to honestly list
the parts not attempted and never claim steps that were not executed.
