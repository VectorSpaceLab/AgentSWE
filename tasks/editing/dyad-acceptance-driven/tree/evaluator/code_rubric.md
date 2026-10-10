# Independent Code rubric — 100 points

Score the frozen Dyad Candidate source and its production seams only. Keep this
axis separate from Agent-loop Result; do not add, average, or infer Code credit
from hidden-case outcomes or broker statistics.

| dimension | points | full-credit anchor | low-credit anchor |
|---|---:|---|---|
| `interface_lifecycle` | 15 | Typed Acceptance preview and attestation IPC, main-owned lifecycle, stable control/session identities, and compatible ordinary Dyad flows | Missing or ad-hoc interfaces, renderer-owned authority, or broken lifecycle compatibility |
| `requirement_mechanism_coverage` | 20 | Production mechanisms cover preview start/read/stop, persistence, ownership, generation fencing, revision and target validation, terminal attestations, and restart recovery | Requirements are prompt-only, test-only, partial, or implemented by a second runner |
| `analysis_evidence_integrity` | 15 | Acceptance claims derive from durable sessions, exact target fingerprints, workspace revisions, terminal counts, immutable receipts, and attestations | Synthetic fingerprints, field presence, logs, or process exit are treated as proof |
| `safety_privacy_side_effects` | 15 | App/chat/run/session ownership, typed validation, credential isolation, bounded disclosure, idempotent starts, and monotonic terminal writes are enforced before mutation or readback | Foreign state can be read or changed, secrets/oracles leak, or retries duplicate side effects |
| `recovery_honest_failure` | 10 | Interrupted, stale, cancelled, drifting, malformed, and infrastructure-limited work converges to an honest non-passing terminal state without resurrection | Restart or late callbacks rerun, overwrite, resurrect, or overclaim success |
| `testability_observability` | 10 | Focused deterministic tests exercise persistence, transaction races, ownership, restart, renderer behavior, and production integration with bounded evidence | Only happy paths are tested or observability depends on private/unbounded data |
| `maintainability_generalization` | 10 | Clear modular TypeScript/SQLite/Git integration generalizes across apps, chats, targets, generations, and existing Dyad modes | Case-specific branches, hard-coded targets, duplicated state machines, or brittle constants |
| `resource_discipline` | 5 | State, logs, subprocesses, filesystem work, and retries are bounded and reuse Dyad’s existing runner and cancellation path | Unbounded growth/work or an unapproved service/process manager is introduced |

For every dimension, require concrete file and symbol evidence from the frozen
Candidate. Behaviorally equivalent implementation choices may receive credit;
mere schema presence or a passing Result case may not.
