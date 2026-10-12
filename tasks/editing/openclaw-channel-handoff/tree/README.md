# OpenClaw channel handoff — Agent-loop Edit benchmark v1

This sibling benchmark measures whether a Builder can edit OpenClaw so its real
lower agent preserves channel handoff correctness under ownership changes,
crash/retry recovery, thread routing, attachment staging, and interaction
completion. The Result axis scores a frozen OpenClaw lower-agent rollout. The
independent Code axis scores the submitted source using the eight required
mechanism dimensions; the axes are reported separately.

The authoritative product snapshot is under `input/repository/`. The Builder
only receives `input/` and `dev_cases/`, and must create its candidate in its
own workspace. Hidden cases and evaluator oracle data are never mounted into a
Candidate.

The real lower entry is the compiled OpenClaw product: `node openclaw.mjs
gateway` starts the Gateway, and the evaluator drives the Gateway's production
RPC (`agent`, `agent.wait`, and, where appropriate, `sessions.send`/`chat.send`).
The lower agent therefore remains OpenClaw's embedded agent and channel/session
runtime; the evaluator does not substitute a generic coding agent.

Lifecycle (the only formal-capable entry is `harbor/formal_one_stop.py`):

1. One evaluator-owned Builder process receives only `input/` and `dev_cases/`.
2. The first accepted Candidate is submitted through a per-run Unix socket and
   evaluated on both public cases through the real OpenClaw Gateway.
3. Redacted feedback is materialized and explicitly acknowledged by that same
   Builder session before another Candidate can be submitted.
4. Each accepted Candidate must have a distinct digest, pass both public
   executions, and receive fresh feedback; the Builder may continue for up to
   ten accepted rounds or stop early, after which the latest accepted Candidate
   is frozen into an immutable canonical tree.
5. Hidden `test_001` through `test_006` may run only after the freeze and the
   Builder-session attestation pass all formal gates.

Run the offline checks with:

```bash
python3 infra/self_test.py
python3 -m py_compile broker/responses_broker.py adapters/materialize_candidate.py lower_agent/launcher.py controller/two_round_controller.py evaluator/public_runner.py evaluator/hidden_executor.py evaluator/test_agentloop_invariants.py code/score_runner.py infra/self_test.py
node --check lower_agent/launcher.mjs
```

Evaluator-owned lifecycle entry points are:

```bash
python3 harbor/formal_one_stop.py --run-dir <lifecycle-output>

python3 evaluator/hidden_executor.py \
  --freeze-manifest <lifecycle-output>/freeze_manifest.json \
  --hidden-root test_cases \
  --output <lifecycle-output>/hidden \
  --broker-endpoint http://127.0.0.1:<port>/v1/responses \
  --formal \
  --builder-attestation <lifecycle-output>/builder_session_attestation.json \
  --runtime-product-manifest <lifecycle-output>/candidate_runtimes/candidate_002.json
```

`formal_one_stop.py` starts separate evaluator-owned Builder and lower-agent
brokers. The lower broker is locked to `gpt-5.6-sol`/`medium`; the Builder
broker is separately locked to `gpt-5.6-sol`/`xhigh`. The Candidate sees only
`broker-only-placeholder`. The old `evaluator/public_runner.py` now requires
`--legacy-probe` and marks its output probe-only; it cannot establish formal
Builder provenance.

The hidden executor validates the immutable latest-accepted-Candidate manifest, same-session
attestation, Candidate runtime digest, all six evaluator-owned cases, and each
case's broker delta, trajectory, result, and attestation. It continues after an
individual Candidate behavior failure so later cases remain observable, but
fails closed on provider/launcher/integrity gates. Neither entry point computes
a formal score.

The construction checks are static only. A bounded simple pilot was then run
on September 2, 2026 for `dev_001`: a private runtime copied the pinned Node
24.15.0/pnpm 11.15.1 environment, started the real OpenClaw Gateway, reached
the product `ready` state, called production `health`, `agent`, and
`agent.wait`, and recorded the evaluator-owned broker trajectory. The broker
received 3 Responses attempts and recorded 3 failures / 0 successes because
this environment did not provide an upstream Responses URL and credential.
The pilot is evidence of runtime wiring and honest failure handling, not a
paper/formal Result; no Candidate freeze or hidden case was started.
Read the earlier evidence under `artifacts/minimal-pilot/`. It remains
historical smoke evidence, not a formal Result. New lifecycle evidence must use
a uniquely named `artifacts/` directory; the runners refuse to overwrite an
existing run. Promotion to formal scoring still requires successful,
independently attributable broker and product evidence.
