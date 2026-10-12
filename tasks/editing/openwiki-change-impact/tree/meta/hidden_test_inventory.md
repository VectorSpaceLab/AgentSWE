# Agent-loop inventory — 2 public dev + 6 hidden

The authoritative native manifests remain evaluator-owned for scoring. This
inventory records the semantic coverage and does not publish expected dynamic
answers, receipt bytes, or hidden oracle values to a Builder.

| ID | Visibility | Product behavior under test | Dynamic/isolated pressure |
|---|---|---|---|
| `dev_001` | public | rename propagation; executable example; preserved anchor and unrelated page; duplicate/lost-response retry | fresh request nonce, tenant `public-team`, generation 1, publication and search identities |
| `dev_002` | public | default behavior change; expired-owner recovery; partial staged state; explicit-option preservation; payload conflict | fresh nonce, tenant `public-team`, generation 2, stale claim and incomplete staging |
| `test_001` | hidden | module move plus compatibility alias; stale-owner restart | independent fixture, generation 4, publication/search roots and expected diagram evidence withheld |
| `test_002` | hidden | return-type migration across transitive pages; corrupted active search recovery | independent fixture, generation 9, search corruption probe and tenant state withheld |
| `test_003` | hidden | removed/deprecated API migration and migration guidance | independent tenant/generation 5, negative Create API control and preserved archive note withheld |
| `test_004` | hidden | partial staging recovery; deleted source path; diagram/link/example convergence | independent short lease, preseeded corrupt publication/search generation withheld |
| `test_005` | hidden | concurrent duplicate commit and stale terminology removal | two workers, one logical request, independent monorepo publication/search roots |
| `test_006` | hidden | ordinary no-op and tenant isolation under prompt-injection/security pressure | security tenant, canary text, no-change expectation and hostile source comment withheld |

Every case has a case-local request, repository fixture, output directory,
nonce, and evaluator-owned oracle. Public and hidden case IDs are structurally
related but do not share dynamic values or expected answer bytes.
