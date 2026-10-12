# Hidden test inventory (oracle withheld)

The inventory fixes six behavioral themes and public interfaces. It omits nonce, OID, receipt, expected state, secrets, assertion answers, and scoring examples. Hidden case inputs/assets remain evaluator-owned.

| ID | theme | dynamic observation | safety/recovery focus |
|---|---|---|---|
| test_001 | admission + bounded DAG | fresh repository identities, leases and concurrent-ready tasks | reject overlap; allow disjoint work; no pre-decision publication |
| test_002 | quarantine/worktree closure | candidate object visibility, worktree registration, cleanup receipts | promote only verified closure; exactly-once hooks/filters |
| test_003 | durable decision prefix | crash after participant prefix and before suffix | report read-only partial state; roll forward exact suffix |
| test_004 | component conflict/ownership | foreign writer, conflicting component and disjoint transaction | preserve external author; no compensating overwrite |
| test_005 | reverse decision + dirty state | staged/untracked/mode/symlink snapshots and reverse CAS | restore exact two-repository state after authorized rollback |
| test_006 | integrity/status compatibility | alias, fence, ledger corruption, obstruction and Aider compatibility | non-blocking status, reject stale/foreign effects, honest failure |

All six are structurally related to the two public cases but have independent runtime values and evaluator-owned oracles.
