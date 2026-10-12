# Rubric: transactional effects and workspace recovery (100 points)

## Execution and validity rules

- An agent that fails to launch, times out, produces no patch, produces a patch that cannot apply once, changes forbidden paths, or fails the strict repository type/product gate receives `0` for the affected evaluation.
- Missing target artifacts or corrupt/unparseable required reports receive `0` at the delivery gate. A missing non-essential auxiliary file does not independently force a zero.
- Valid but incomplete implementations receive ordinary behavioral scores. Where a newly introduced module is imported, a missing newly introduced module is a behavioral failure, not a global validity gate.
- Score only the observable behavior of the final patch. Do not score implementation style, framework choice, prompts, internal architecture, or similarity to a reference implementation.
- Every hidden case has stable assertions totaling 100. The benchmark score is the arithmetic mean of the 6 complete case scores.

## 1. Authority, scope, and stale-response fencing (20 points)

Objects: ledger lease/generation authority, sync term/tab incarnation, workspace transaction fencing, and exact backend/conversation scope.

- Complete: one compare-and-set winner per race; every callback and awaited workspace/transport response re-validates the full authority; a successor may complete while the old result becomes a non-throwing suppressed observation; wrong scopes stay isolated.
- Middle: basic takeover works, but a restart/incarnation/late-response path can still invoke a sink, apply a manifest, or write a cursor.
- Low: owner text, wall clock, a remote cursor, or a term alone grants authority; stale work overwrites successor state or crosses backend scope.
- Major error: two winners, old-generation settlement, a late workspace apply, foreign-scope mutation, or a cursor advance under stale authority.
- Do not penalize token shape or equivalent internal compare-and-set organization when the observable fence holds.

## 2. Workspace planning, transfer, and ordered commit (30 points)

Objects: immutable manifests, the deterministic three-way plan, verified chunks, the remote ledger commit, the local CAS apply, and the final workspace cursor.

- Complete: validates bounded canonical manifests; uses the explicit common base; propagates only single-sided edits/deletes and blocks two-sided conflicts; probes the destination and verifies transferred digests; the remote exact CAS runs as a recoverable ledger effect; the local exact CAS follows; the cursor publication is last.
- Middle: conflict-free additions converge, but a delete/type conflict, a corrupt chunk, response loss, or one ordering edge is incomplete.
- Low: picks the newer side, overwrites differing paths, copies unverified bytes, repeats the remote commit, applies partially locally, or publishes early.
- Major error: data loss, path escape, acceptance of a digest mismatch, replicas diverging after reported completion, or a second logical commit after response loss.
- Do not penalize chunk size, target revision encoding, batching, or a private Merkle representation when identity is deterministic and the public semantics hold.

## 3. Ledger outbox and crash honesty (20 points)

Objects: the schema-v3 immutable ledger and the queued/claimed/applied/completed delivery states, including the workspace remote commit as an unsafe effect.

- Complete: the claim is readable before the callback; a rejected callback leaves an honest released recovery state; all effect and workspace crash boundaries recover from durable evidence; an uncertain remote commit is reconciled before execution; duplicates settle once.
- Middle: ordinary execute/replay works, but one rejection, apply-only, unresolved reconciliation, or post-commit crash path is ambiguous.
- Low: a callback return is treated as durable completion, claims exist only in memory, or a blind restart re-executes unsafe work.
- Major error: a duplicated unsafe callback, cross-delivery settlement, loss of completed evidence, an unhandled stale rejection, or wrong completion after response loss.
- Extra immutable revisions and bounded audit entries are acceptable when the state meaning and callback counts stay correct.

## 4. ABA, events, migration, and compaction (10 points)

Objects: pause/resume/cancel generation transitions, runtime event de-duplication, v2 migration, integrity fallback, and exact-scope namespace preservation.

- Complete: generations block ABA; cancel is terminal; duplicate/reordered events never repeat optional sinks; v2 migrates once; corrupt/future records fall back; compaction cannot enumerate the sync or workspace transaction namespaces.
- Middle: the current-schema flow works, but one migration race, cleanup failure, or late terminal binding is incomplete.
- Low: generations can be bypassed through sync/workspace terms, migration repeats, corruption becomes current, or compaction crosses scope/namespace.
- Major error: cancelled work revives, legacy/foreign/workspace records are removed, or a failed cleanup invalidates the committed latest revision.
- Do not penalize benign over-retention after an injected removal failure when the subsequent exact-scope cleanup is real.

## 5. Least privilege and content-safe projections (10 points)

Objects: status/support grants, store/UI data, sync notifications, and workspace recovery projections.

- Complete: wrong audience/scope, expired, revoked, and generation-changed are uniformly refused; limits are real; no prompts, credentials, raw errors, tokens, paths, content/root/chunk/plan digests, bytes, or tab/incarnation identities leak.
- Middle: the grant lifecycle is correct, but a non-secret identity/digest or truncation flag leaks/is wrong.
- Low: status exposes checkpoint bodies, tokens stay raw, or workspace paths/content fingerprints appear publicly.
- Major error: any credential, grant value, prompt tail, authority token, workspace bytes/path, or content digest is exposed. The redaction assertions impose the documented zero cap.
- Safe phase codes, bounded counts, scopes, revisions, and benign algorithm names must not lose points.

## 6. Production integration and compatibility (10 points)

Objects: production services, the dispatcher, the sync coordinator, the workspace factory, the store, status components, and existing APIs.

- Complete: factories wire browser storage and ledger operations so that callers cannot bypass them; the dispatcher and sync resume from the post-workspace cursor; store/UI convergence stays authorized; ordinary APIs and focused upstream tests are unchanged.
- Middle: the injectable modules work, but one production factory, store notification, dispatcher handoff, or response-loss path is incomplete.
- Low: adapter-only implementation, a caller-replaceable production ledger, duplicated ordinary/terminal effects, or a regression of an existing API.
- Major error: the production workspace factory uses a different ledger/scope, the cursor skips unfinished workspace recovery, or ordinary checkpoint-free conversations break.
- Extra focused tests or private helpers lose no points when the allowed paths and public behavior stay compatible.

## Case-local integrity caps

Assertions are scored independently, then the documented local caps are applied. `test_001` is capped when the ordered workspace commit or takeover fencing fails. `test_002` is capped when workspace crash recovery or uncertain remote commit reconciliation fails. `test_003` keeps the ABA/cancellation cap. `test_004` keeps the scope and absolute-edit caps. `test_005` keeps the migration and compaction caps. `test_006` is capped when production workspace commit recovery or takeover fencing fails. These are behavioral score caps, not suite validity gates. The workspace availability cap is 30 for `test_001`, `test_002`, and `test_006`: a defining transaction/fencing failure prevents a passing score while leaving credit available for independently observed behavior. Assertion weights and all other case-local caps stay unchanged.
