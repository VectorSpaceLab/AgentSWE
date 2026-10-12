# Hidden case 5: concurrent duplicate delivery in a single repository

Maintain the two-package monorepo after only Alpha changed its public
`mode()` result. Two production CLI processes with the same tenant,
request, generation, and payload start concurrently with different owners
and response paths.

Serialize the logical transaction so that one process commits and the
other returns receipt-backed duplicate evidence. Preserve a single
immutable receipt, one generation advance, no live claim/lock/temp files,
the corrected Alpha documentation/example, and byte-identical Beta and
overview pages despite the same-named symbol. Neither process may time out,
corrupt the other's response, or leak example state.

Both concurrent processes select the same static publication and search
roots. Exactly one complete active release/index pair may become visible,
with matching generation and digest; each release must have duplicate
confirmation and byte-stable index bytes, while Alpha's stale search term
disappears and Beta's remains.

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
