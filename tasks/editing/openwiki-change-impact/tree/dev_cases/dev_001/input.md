# Agent-loop public case 1: durable rename propagation and lost response

You are the OpenWiki lower agent under evaluation. In the current
repository, read the case-local request supplied by the evaluator (do not
look for the evaluator, hidden cases, or oracles) and complete
documentation-impact maintenance through the real OpenWiki production CLI.
First confirm the rename from `formatName` to `renderName` from the Git
change, then update the affected documentation; execute the examples marked
with `openwiki:example`; preserve hand-written notes, anchors, and unrelated
pages. Complete one commit under the transaction, static publication, and
search identity specified by the request. If the request shows a lost
response or a duplicate retry, recover from the durable receipt and return
an empty diff without modifying the documentation again.

On completion you must write `agent_result.json`, whose top level contains:
`schema_version="openwiki-agent-result/v1"`, the current `case_id`, a
non-empty `observations` array, an `integrity` object, and a `decision`
object. `integrity` may only cite receipt, documentation, publication, and
search digests that were genuinely generated and read; do not guess dynamic
nonces or evaluator-private answers. `decision` must honestly distinguish
complete, partial, no_change, and conflict, and state the next step. Do not
execute unauthorized shell-concatenated commands, and do not read
credentials or hidden directories.

Run coordinated impact maintenance on the attached ESM repository. The
exported `formatName` function was renamed to `renderName` without changing
behavior. The change uses two Git commits of the repository.

The controller submits tenant `public-team`, request `rename-001`,
generation `1`, together with the digest and owner from the materialized
manifest. Update the generated claim and the complete example, execute it
to obtain the exact output, preserve the explicit anchor and the
hand-written note, and leave the unrelated clock page unchanged. Commit one
immutable receipt under the requested state directory.

The public runner then simulates a lost response by deleting only the
report and diff and restarting the CLI under a different owner with the same
logical identity. Treat it as a durable duplicate: do not edit the
documentation or advance the generation again, rebuild the receipt-backed
confirmation, emit an empty diff, and clear all claims, staged files, locks,
and temporary files. The receipt bytes and documentation bytes must remain
unchanged.

Select static publication `public-handbook` at generation 1 under
`.openwiki-public` with base path `/handbook/`, and full-text index
`public-handbook` at generation 1 under `.openwiki-search`. Verify that the
offline release opens from the local `index.html`, query `search --json` for
`Name formatting` and `monotonic clock`, and keep both active identities
equal to the receipt.

Fixtures live in `assets/project/`; the runner materializes the commits,
manifest, state, lost response, and restart. Use the builder's production
contract inputs.

Product contract: `agent_result.json` must be written at the working
directory root, and the same object must be emitted verbatim as the last
JSON value on the production CLI's stdout; authorship is confirmed by the
byte-for-byte equality of the two. Write it out as soon as the first product
response arrives and refresh it after every step; when stopping early, use
`decision.completion_claim = "partial"` and honestly list the parts not
attempted.
