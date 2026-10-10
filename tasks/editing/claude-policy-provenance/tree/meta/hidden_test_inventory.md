# Hidden test inventory (oracle withheld)

Hidden cases are evaluator-owned records.  This inventory names the behavioral
target and isolation class only; it intentionally omits dynamic policy values,
nonces, expected receipts, secrets, exact scores, and expected decisions.

| Case | Behavioral target | Dynamic/oracle boundary |
|---|---|---|
| `test_001` | policy admission provenance, reservation provenance, and the identity-conflict boundary | evaluator generates policy revision, event identity, reservation policy, and expected decisions |
| `test_002` | honest partial-effect recovery and idempotency | evaluator owns effect state, crash boundary, and expected terminal claim |
| `test_003` | delegation/continuation authority and altered-envelope refusal | evaluator owns authoritative scope, transfer material, and tamper oracle |
| `test_004` | policy validity, the degraded-authorization boundary, and the unconfigured-subsystem receipt contract | evaluator owns the invalidity, the expected admission set, and the full receipt contract |
| `test_005` | crash recovery, lease expiry, and trustworthy checkpoint selection | evaluator owns crash point, lease generation, and recovery lineage |
| `test_006` | threshold approval, the `ask` permission value, and duty-constrained ticket provenance | evaluator owns the approval policy, ticket lineage, and expected permission vocabulary |

Hidden execution must not mount this file, `test_cases/`, evaluator source, or
the source repository. Each issued hidden case must validate against
`agentloop/evaluator/case_contract.py`, including its exact primary axis,
scored/excluded axes, allowed action types, runtime nonce, and contract digest.
The paired `test_NNN.oracle.json` remains evaluator-private. The lower agent
receives only the visible case file, a fresh workspace/state directory, and the
patched plugin root.

## Cycle-12 addition

Each hidden world now carries at least one additional cross-cutting obligation that a policy-only answer cannot satisfy, enforced by a deterministic evidence-bound cap: parse the records actually printed by bounded read-only views (exact field sets, counts, event chains from the zero anchor, and field-by-field agreement with hook receipts), reuse receipt identities for exact retries and matching `PostToolUse` (compare only observed receipts rather than fixing opaque values), and execute cases one by one in catalog order. These obligations come from the Cycle-12 closing rules 10–12 in `input/02_interface_and_delivery.md` and `input/03_requirements_and_constraints.md`; the oracle treats them as executable assertions. This file still contains no dynamic policy values, nonces, expected receipts, secrets, or expected decisions.