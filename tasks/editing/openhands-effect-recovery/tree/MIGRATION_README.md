# OpenHands effect recovery Agent-loop sibling (Stage A)

This sibling is the migration foundation for owner-13. The authoritative
source remains read-only at
`@@AGENTSWE_EDITING_SOURCES@@/13-edit-openhands-effect-recovery`.
Only this directory is writable for this task.

The target lower agent is the patched OpenHands Agent Canvas TypeScript product:
the evaluator driver imports the candidate's recovery adapter and routes its
model-selected bounded actions through the existing conversation/runtime
boundary. It is not an external Codex replacement. The evaluator-owned broker
forces `gpt-5.6-sol` with reasoning `medium`; Candidate-visible credentials are
placeholders and broker stats record calls, failures, and tokens.

The lifecycle is bounded rather than fixed at two submissions: one continuous
Builder session may submit up to ten distinct Candidates, each accepted
submission runs both public dev cases and returns fresh authoritative feedback,
and Builder exit or the round limit freezes the latest accepted Candidate.
Hidden cases may start only after that freeze. Result is the lower-agent
behavior axis. The eight-dimensional Code rubric is an independent axis with
weights 15/20/15/15/10/10/10/5.

Stage-A commands are deliberately non-heavy:

```bash
python3 self_test.py
python3 -m py_compile adapters/materialize_candidate.py evaluator/broker/candidate_broker.py evaluator/controller/two_round_controller.py evaluator/code_axis/run_code_axis.py lower_agent/openhands_lower_agent.py self_test.py
node --version
npm --version
npx --no-install tsc --version   # if the task-local dependency tree is available
```

The only lifecycle entry point capable of producing reviewed Agent-loop
evidence is:

```bash
python3 harbor/formal_one_stop.py \
  --run-dir <new-empty-run-dir> \
  --run-formal
```

It starts independent Builder (`gpt-5.6-sol/xhigh`) and lower-agent
(`gpt-5.6-sol/medium`) evaluator-owned brokers, and refuses a non-empty run
directory. `evaluator/controller/minimal_smoke_controller.py` is deprecated
and fail-closed because its former external Candidate path was not a valid
same-session lifecycle.

These are infrastructure/syntax/build smokes, not paper-quality Agent-loop
Results. No successful broker calls exist until the separately reviewed Stage-B
simple pilot is scheduled. Accordingly the current state is `PARTIAL`, never
`READY`.

The simple pilot should use a known-good or baseline-compatible Candidate, one
public dev case, then freeze and run one hidden smoke. Expected budget is one
Node/Vitest process, 2–5 minutes of broker/runtime time, approximately 2–8
successful lower calls depending on the action plan, 1–2 GiB PSS for the driver,
and up to 4–8 GiB PSS plus 1–3 GiB disk if the pinned OpenHands dependency/build
path is exercised. Full Builder up-to-ten-round × six-hidden formal evaluation
is not scheduled by this sibling.
