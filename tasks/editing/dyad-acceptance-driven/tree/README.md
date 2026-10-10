# Dyad acceptance-driven Agent-loop v1 migration

This is the stage-A migration sibling for owner-19. The authoritative source
is preserved read-only at
`@@AGENTSWE_EDITING_SOURCES@@/19-edit-dyad-acceptance-driven`.

The sibling keeps the source public input/dev material and adds an evaluator-
owned lower-agent protocol. The lower product is Dyad itself: the planned
launcher starts `npm start`, whose real entry is
`scripts/start-supervisor.mjs` and Electron Forge; the product task then enters
through typed `chat:stream` in acceptance mode. No external Codex or generic
coding agent stands in for Dyad.

## Protocol

The evaluator broker is the only owner of the real credential. Candidate code
gets `broker-only-placeholder`; every request is rewritten to
`gpt-5.6-sol` with reasoning `medium`, and stats record calls, failures and
token counts. Candidate materialization applies a repository-relative patch
once, runs the Dyad type gate when dependencies are available, and keeps the
frozen digest immutable.

The controller permits one through ten distinct accepted Candidate digests in
one recorded Builder session. Every accepted submission runs both public dev
cases and produces fresh feedback with a SHA-256 receipt. A later submission
must acknowledge the latest same-session feedback digest, and a passing dev
round is feedback only: it does not automatically freeze. When the Builder
exits or the accepted-round limit is reached, the latest structurally valid
Candidate is frozen. Hidden evaluation is rejected until that freeze manifest
exists and is checked, then all six hidden cases are dispatched through the
real lower runner. Result and the fixed eight-dimensional Code score are
separate axes.

## Isolation and dynamic facts

Builder visibility is limited to `input/` and `dev_cases/`. Hidden inventory,
evaluator code, dynamic target/revision values, old runs and credentials are
evaluator-owned. Each case receives fresh case-private state; its oracle checks
typed IPC state, event monotonicity, workspace revisions, fingerprints,
attestation/receipt linkage and final artifact claims against actual runtime
evidence. Prompts must not embed expected statuses, hidden fixture content or
oracle values.

## Smoke boundary

`smoke/self_test.py` and Python/Node syntax checks are static infrastructure
checks. They do not run a Builder, do not prove Dyad lower-agent behavior, and
do not constitute a paper/formal Result. A simple pilot still requires one real
dev lower-agent case, successful broker calls with the locked protocol, and one
hidden case after freeze. Until then this sibling is `PARTIAL`, not `READY`.

Rust is not an applicable product language for Dyad; no Rust build is claimed.

## Verifiability and driveability

The Acceptance surface is judged on what a caller can verify without guessing.
Seven cross-cutting obligations are published in `input/02` and `input/03` and
are decided by the evaluator from product returns alone: the current Acceptance
target is published so the caller never guesses a field of it, the accepted
target survives verbatim into the session, `testFingerprint` is the SHA-256 of
the admitted target file's raw bytes, `lastEventSequence` never decreases,
repeat reads of a terminal session and of an immutable attestation return
identical bytes, `resultDigest` is the published canonical digest of the
terminal facts, and a non-passing attestation never carries a run to `passed`.
Both public dev rounds report these seven verdicts back to the Builder, so they
are a calibration channel rather than a hidden trap.
