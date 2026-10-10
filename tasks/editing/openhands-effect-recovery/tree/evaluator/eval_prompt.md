# Evaluator instructions

Evaluate one hidden case from `input.md`, the synthetic assets, the patched
Agent Canvas final product, the executable case results, and the global
rubric.

1. Validate the delivery/build/type/product entry points before behavioral
   scoring.
2. Run only the current evaluator-owned case in an isolated process. Treat a
   timeout/crash as a behavioral failure of this case; do not invalidate
   completed unrelated cases.
3. Score every stable assertion on observable durable storage, callback
   counts, service/store state, browser event aggregation, or the rendered
   UI.
4. Apply every documented case-local cap after the raw assertion subtotal.
   For `test_001`, `test_002`, and `test_006`, the workspace availability cap
   is 30 when either named critical assertion fails; keep scoring the
   independent assertions before applying that cap.
5. Cite concrete evidence for every inference: assertion IDs, durable
   revisions or state transitions, callback counts, backend/tab scopes,
   service method output, storage snapshots, or visible element text.
   For migration and late-settlement cases, cite the winner
   revision/authority, whether the stale writer published anything, and
   whether the successor completion existed before the old callback
   settled.
   For concurrent delivery, callback rejection, compaction, and storage
   cases, also cite the independent claim/delivery identities, the durable
   post-rejection state, the exact retained scope keys/removal-failure
   anchors, the active watch grant, and whether the replay wrapper caused
   any second ordinary-event side effect. For dispatcher cases, cite the
   returned durable recovery cursor, the active session generation, the
   stale-session ledger/sink call counts, the terminal output count, and
   whether the stale close affected the replacement.
   For sync coordinator cases, cite the durable term/incarnation winner,
   pull cursor and call counts, the ledger cursor before/after response
   loss, the accepted envelope count, the stale-response dispatcher/sink
   counts, the wrong-scope notification effect, and whether
   notifications/records contained disallowed events or secret material.
   For workspace cases, cite the base/local/remote plan result, conflict
   and chunk callback counts, remote commit/reconcile counts, the local
   apply pre-image, permission changes, the durable workspace phase, and
   the ledger cursor before/after. Confirm that the remote commit preceded
   the local apply and that `workspace_reconciled` was last. For
   projections, search for paths, bytes, all digest classes, raw errors,
   tab/incarnation identities, credentials, and authority tokens.
6. Ignore source organization, code style, candidate tests, framework
   choice, prompts, and similarity to any reference implementation.
7. Redact/bound all evidence. Never echo fixture credentials,
   claim/fencing/grant tokens, prompt-like tails, raw effect bodies, private
   paths, content bytes, or workspace/content/chunk/root/plan digests.

Return: validity, case status, assertion max/earned/passed/evidence, raw
subtotal, applied local cap, final `0..100` score, major errors, command
duration/peak PSS, and a concise observable assessment. A missing evaluator
result is an integrity error and must not be synthesized into a valid
zero.
