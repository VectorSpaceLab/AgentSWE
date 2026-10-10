# Agent-loop case inventory (public contract)

The public cases expose task shape and legal observations, not hidden expected
decisions or evaluator oracle values. Hidden inventory is intentionally a test
plan only; it contains no expected answer, dynamic secret, or fixture body.

## Public development cases

| case | OpenHands product behavior | evaluator-owned dynamic facts | required lower-agent artifact |
|---|---|---|---|
| `dev_001` | Recover one remote effect after a lost response, then apply a matching local workspace revision without double commit. | fresh scope, effect/delivery IDs, manifest version, response-loss flag, opaque receipt handles | `agent_result.json` with bounded observations, state enum, receipt references, and honest next action |
| `dev_002` | Reconnect a conversation after lease takeover while a stale runtime event and a two-sided workspace edit race. | fresh backend/conversation/tab incarnation, event cursor, lease epoch, conflict path label, delayed-event schedule | same schema; must distinguish stale/blocked/conflict from completed and bind claims to the current rollout |

## Hidden test inventory (oracle withheld)

| case | behavior family | observable dimensions |
|---|---|---|
| `test_001` | lost chunk response and resumable workspace transfer | chunk probe/write counts, digest verification, ordered remote/local/cursor commit, conflict safety |
| `test_002` | uncertain remote commit and crash-boundary recovery | outbox stages, exactly-once reconcile, callback suppression, final cursor ordering |
| `test_003` | pause/resume/cancel ABA and lease takeover | run generation, fencing, stale callback/event rejection, terminal state |
| `test_004` | minimal inspection grant and scope isolation | expiry/revocation/audience checks, bounded truncation, redaction of path/content/token material |
| `test_005` | legacy v2 migration and compaction | one-way migration, foreign lineage preservation, precise namespace cleanup, corrupted/future fallback |
| `test_006` | dispatcher/sync takeover and production factory integration | durable cursor reconnect, stale session sink count, tab incarnation, UI/store convergence |

Each hidden case is a fresh evaluator-owned scenario. The six rows are not a
claim that the legacy native suite is an Agent-loop Result; they are behavioral
targets for the model-driven Canvas lower process.
