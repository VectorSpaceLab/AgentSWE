# DeepCode Agent-loop migration plan

## Scope and authoritative boundary

This sibling is the only writable benchmark directory for owner-17. The
authoritative source, the 0830 staging tree, and historical formal runs are
read-only. The copied `input/repository` remains the product baseline; the
migration adds evaluator-owned orchestration around a Builder-produced patch.

## Six entrypoint and design questions

1. **Real product entry.** The lower product is the Candidate's patched
   `input/repository/deepcode.py` (or its installed console equivalent),
   invoked as `python deepcode.py exec <task> --json`. This enters
   `cli.exec_cli`, `cli.headless_turn`, `DeepCodeApplication`, the durable
   Turn/AgentRunner, and the Candidate's own traceability CLI/modules. The
   evaluator must never replace this with a generic Codex or an evaluator
   implementation of the task.
2. **Model surface.** DeepCode resolves an OpenAI-compatible connection through
   `core.providers.openai_compat`. The launcher supplies an isolated
   `DEEPCODE_HOME` profile whose custom API base is the evaluator broker. The
   broker overwrites model and reasoning settings to `gpt-5.6-sol` and
   `medium`, regardless of Candidate configuration or task text.
3. **Task/action/state/artifact.** Each case gives the Agent a fresh local
   project, asks it to use the product's traceability/revision/execution/audit
   interfaces, and requires `agent_result.json` in the case workspace. Tool
   calls and process output are raw trajectory evidence; only the Agent's
   authored JSON is the final artifact. The evaluator also records resulting
   capsule/revision/plan/audit state without treating source symbols as proof.
4. **Dynamic oracle.** The evaluator creates a fresh nonce, tenant/project
   scope, capsule contents, policy version, operation IDs, and corruption or
   response-loss event per case. Expected semantic facts stay in the evaluator
   process and are compared with observed state/receipts. The Agent sees only
   the case project and operational task, never `case.json`, hidden manifests,
   evaluator code, expected values, or historical output.
5. **Isolation.** Builder sees only `input/`, the two public case prompts and a
   writable public repository copy. Candidate runs use a fresh repository copy,
   workspace, `DEEPCODE_HOME`, case project, and output directory. Hidden cases
   are materialized after freeze in separate evaluator-owned directories. The
   broker holds the real credential; Candidate receives a placeholder token.
6. **Risk and cost.** DeepCode has a large Python dependency surface and may
   require a prepared offline environment. A simple pilot should budget one
   lower-agent dev run plus one hidden smoke, each up to 600 seconds, with two
   small model calls expected per run and at most 4 GiB/4 CPU in a container if
   containerized. Builder feedback is intentionally not started in
   Stage A. The main risks are missing dependencies, provider incompatibility,
   model calls that do not reach the broker, and a Candidate that changes the
   CLI without producing a truthful artifact; each is classified separately
   from Candidate behavioral failure.

## Case migration (not a byte-stream copy)

The public cases exercise affine capsule review/promotion and paused selection
execution. Their dynamic task is to operate the patched DeepCode CLI and return
evidence, rather than call a fixed native test. The six hidden inventories cover
concurrency/idempotency, lease fencing, tenant isolation, corruption
reconciliation, cancellation fencing, and policy-change blocked recovery.
Their exact oracle and generated values are evaluator-owned.

## Candidate lifecycle

`CandidateController` accepts a materialized delivery, computes a digest,
builds it, runs both `dev_001` and `dev_002`, and returns structured feedback.
It permits up to ten distinct accepted Candidate digests, with fresh feedback
after every acceptance. The Builder may stop early; the latest accepted
digest is frozen on Builder exit or when the ten-round limit is reached.
Hidden execution raises a protocol error before freeze and otherwise receives
only the frozen artifact. Infrastructure-invalid attempts do not consume a
capability round.

## Simple-pilot estimate

Expected Stage-B smoke budget per sibling: 1 Builder-free Candidate materialize
and Python build gate (under 2 minutes warm), one public lower-agent run
(typically 2-10 minutes, 1-5 broker calls, 10k-60k output/input tokens), and
one post-freeze hidden smoke (2-10 minutes, 1-5 calls). Reserve 4 CPU, 4 GiB
RAM, 8 GiB writable scratch, and one provider credential in the broker only.
No estimate here is a completed run; this Stage-A sibling has no successful
broker call and therefore cannot be marked READY.

## Stage-A acceptance gates

- source and repository digests are recorded;
- two public prompts and six hidden inventory records exist;
- launcher names the real DeepCode entry and enforces model/effort;
- broker stats have calls/failures/tokens and redact credentials;
- up to ten distinct accepted Candidates → two dev evaluations and fresh
  feedback after each acceptance → latest-accepted freeze on Builder exit or
  round limit is executable;
- hidden-before-freeze is rejected;
- Result and Code axes are separate;
- static self-test and language smoke pass, while real behavior remains
  `PARTIAL_STATIC_ONLY` until a pilot observes successful broker calls.
