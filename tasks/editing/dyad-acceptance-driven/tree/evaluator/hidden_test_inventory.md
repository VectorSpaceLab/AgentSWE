# Hidden test inventory (oracle withheld)

The inventory is intentionally structural. Dynamic values, exact fixtures,
expected terminal states, and the scoring oracle are generated and retained by
the evaluator at runtime. Each hidden case proves one distinct hard capability
and fails closed: a capability that is not demonstrated in observed product
behaviour is not credited, whatever the rollout narrates.

| id | hard capability | adversarial element | required evidence |
|---|---|---|---|
| test_001 | a terminal `passed` proof that is byte-true and survives a cold restart | the exact target must be read from the product; the fingerprint is checked against the admitted target bytes; the proof is re-read in a fresh process | fresh fingerprint equal to the target bytes, pinned revision triple, attestation bound to session, identical attestation after restart, no rerun |
| test_002 | idempotent retry after a lost response plus operation-identity conflict detection | first start response is dropped after product acceptance; a later command reuses the caller-stable operation identity with different content | one runner invocation, same session recovered, typed conflict rejection, unchanged session identity/sequence, exactly one attestation |
| test_003 | denial without disclosure, recorded in an auditable append-only ledger | stale generation, foreign owner control and unauthorized read against a live authorized session | typed denials, authorized snapshot unchanged except the ledger, three bounded ledger entries with no echoed foreign identifiers, no disclosure |
| test_004 | content-addressed revision drift permanently invalidates a green runner result | in-flight target mutation under a delayed runner, then byte restoration | invalidated terminal state with a bounded reason, `finishedRevision` different from `startedRevision`, attestation invalidated, no resurrection and no rerun |
| test_005 | cancellation outranks a late callback and a duplicate stop | delayed runner, late terminal callback after cancel, duplicate stop request | cancelled terminal state, late callback observed and ignored, non-decreasing event sequence, a single attestation with `cancelled` outcome |
| test_006 | cold restart reconciles interrupted work honestly and stays compatible | process kill in a fresh PID namespace while the session is non-terminal | honest non-passing terminal state, preserved run identity with monotonic generation/control sequence, attestation matching the recovered status, ordinary chat leaves acceptance bytes untouched |

Hidden prompts must provide only the case task and result schema. They must not
contain these rows, oracle values, expected status, evaluator paths, or fixture
content.

## Cross-cutting contract (every case, oracle withheld)

Besides its own hard capability, every case — hidden and public alike — decides
the seven cross-cutting Acceptance obligations published in
`input/02_interface_and_delivery.md` ("Independently verifiable product contract") and
`input/03_requirements_and_constraints.md` items 23–26: a published current
target, a target preserved verbatim into the session, a fingerprint that is the
admitted target bytes, a never-decreasing event sequence, byte-stable terminal
and attestation reads, a recomputable `resultDigest`, and an acceptance gate
that a non-passing attestation cannot open. They are decided from product
returns the rollout already produced — including the evaluator's own repeat
reads — so they cost the lower agent no extra turn, and any one of them unmet
caps the case's Result at 28.
