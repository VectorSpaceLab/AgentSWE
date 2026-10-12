# AI Scientist Agent-loop migration plan

## Audit conclusion

The authoritative product is a Python 3.11 AI Scientist v2 snapshot. Its real
production surface begins at `input/repository/launch_scientist_bfts.py`, which
dispatches experiment search, plotting, write-up and review; model calls are
implemented through `ai_scientist.llm` and the OpenAI-compatible tree-search
backend. The requested Edit adds the controlled `claim_verification` release
route and must preserve the ordinary one-shot route. The lower Agent therefore
executes the candidate's own `ai_scientist.claim_verification` entrypoint (or
the edited launcher compatibility route), never an external generic Codex.

## Six entrance/isolation/oracle answers

1. **Real product entrance.** Candidate code is materialized from the exact
   source repository and launched through its own Python module. The adapter
   only creates a fresh process/workspace and supplies the case path.
2. **Model integration.** The product's existing OpenAI-compatible Chat
   Completions calls are routed to the evaluator broker. The broker overwrites
   model and reasoning effort to `gpt-5.6-sol`/`medium`; the Candidate sees only
   `broker-only-placeholder`. A Responses route is also accepted for candidates
   that implement the new surface that way.
3. **Task/state/artifact.** The user task is a release-gate decision over local
   claim/evidence inputs; tool actions are prepare/status/commit/verify/cancel
   and replay/worker operations; state is the durable session/budget/outbox/
   provenance receipt; final artifacts are the five release files plus the
   model-authored `agent_result.json` contract.
4. **Dynamic oracle.** `case_service.py` creates a nonce, release decision,
   worker policy and rollout digest at evaluator time. Only the non-secret case
   payload reaches the Candidate. The oracle JSON remains evaluator-owned and
   is consumed only by `result_evaluator.py`.
5. **Expected Edit effect.** The edited product should improve evidence-bound
   scientific decisions, exactly-once release/recovery, durable provenance,
   honest partial-worker handling and cross-artifact digest safety. A model that
   merely emits a plausible report without durable receipts receives little
   Result credit.
6. **Oracle protection.** Candidate and Builder mounts contain no evaluator
   source, hidden inventory, oracle JSON, credential or prior run. The launcher
   makes a fresh workspace; broker stats and oracle paths stay evaluator-side.

## Lifecycle and isolation

The controller accepts between one and ten distinct Candidate snapshots in a
single Builder session. It runs both public dev cases and returns fresh,
digest-addressed feedback after every accepted submission; each later
submission must acknowledge the immediately preceding feedback. Builder exit
or the configured round limit freezes the latest accepted snapshot, and only
then does `run_hidden.py` open hidden execution. Build failures are Candidate
failures; provider, credential, mount and evaluator failures are
infrastructure-invalid/N/A.

## Cost and risk estimate for the simple pilot

The pilot should use one already-built or minimally patched Python candidate,
one lower-agent dev case and one hidden smoke after freeze. Budget for 2--4
successful lower-model calls per dev case, roughly 8k--32k input/output tokens
total, one broker process, one short Python process per case, and under 1 GiB
RSS. Do not run the full Builder up-to-ten-round loop or six hidden cases in Stage A.
The main risks are that the source snapshot currently has no
`ai_scientist.claim_verification` module, optional runtime dependencies are not
installed in this shell, and the product's full BFTS route can execute generated
code. Those are explicit pilot gates, not reasons to fake calls or scores.

## Stage-A acceptance and unresolved risks

Static protocol self-test must pass; Python compile/build smoke must pass; no
formal Result is claimable until a real lower invocation yields successful
broker calls with the locked model/effort. Node and Rust are not product
languages for this source and are recorded as not applicable. The independent
Code runner remains separate from Agent-loop Result and uses the fixed
15/20/15/15/10/10/10/5 weights.
