# Hidden test inventory

The following inventory is evaluator-owned design metadata. Exact dynamic
values, event timing, route identifiers, tokens, attachment bytes, and oracle
decisions are generated at runtime and are not placed in hidden case inputs.

| id | unseen behavior family | dynamic variation | required lower-agent evidence |
|---|---|---|---|
| test_001 | competing owners on one direct route | simultaneous claim attempts and lease generations | exact route, winning owner, no duplicate send |
| test_002 | revocation during queued delivery | grant rotation between enqueue and pull | denial/expiry receipt and no stale control |
| test_003 | delayed provider acceptance | accepted-without-message-id then later reconciliation | accepted vs verified distinction, one post |
| test_004 | thread route migration | direct-to-thread migration with a new session key | thread/account/peer continuity and transcript binding |
| test_005 | attachment crash and resume | missing final upload response plus reordered receipts | exact block offsets, one upload, no bytes in inspect |
| test_006 | callback replay after gateway restart | late callback, compaction checkpoint, duplicate completion | idempotent completion and correlated reply without token leakage |

No hidden row is a per-case oracle. The evaluator computes the truth from its
private runtime event log.
