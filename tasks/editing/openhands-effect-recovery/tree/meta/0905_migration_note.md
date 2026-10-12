# 0905 Edit repair migration note — OpenHands effect recovery

Date: 2026-09-05

This P0 sibling remains `REPAIR` pending a fresh current-code smoke bound to
the current sibling digest. The earlier September 4, 2026 smoke is retained as
historical evidence and is not upgraded after the current digest-bound gate.

The formal path now proves controller execution through
`lower_agent/openhands_lower_agent.py`; the native suite is retained only for
diagnostics and evidence collection. The Builder lifecycle is generalized to
up to ten accepted submissions with both dev cases after each accepted digest,
no automatic freeze on `dev mean > 60`, and six hidden cases after freeze.
Formal Result scoring is delegated to the shared independent semantic judge.
Code scoring is routed through the evaluator-owned wrapper around the unchanged
Create judge so the root-owned credential never needs to be read on the host.

Offline negative controls and the native harness self-test passed. The
provider-free pilot contract now prepares a real lower-product run for
`test_001` and `test_002`,
an evaluator-owned `gpt-5.6-sol/medium` broker, model-authored artifacts,
dynamic oracle-isolation summaries, and an independent evaluator-owned
`gpt-5.6-sol/xhigh` Result judge with one logical request per case. The lower
broker completed two calls; the xhigh broker completed two responses, with one
provider timeout transport attempt excluded as infrastructure evidence. Both
cases were candidate-valid and cleanup was attested complete. Scores were
`test_001=20` and `test_002=15`; these are smoke evidence only and are not formal
benchmark results. A new paid-provider smoke has not been claimed in this
repair pass.

## September 6, 2026 lower-semantic repair

The lower execution boundary was tightened in the current sibling. The lower
driver now runs a bounded, iterative model interaction: each response selects
one action from the public recovery adapter or the production `ConversationService`
dispatcher/sync/workspace factories, the selected arguments are resolved only through
case-local runtime references, the Candidate TypeScript product method is invoked, and
a sanitized product observation is returned to the next model turn. The evaluator records
`trajectory.json` after every product action. A second lower-model response authors
`agent_result.json`; the driver and the Python launcher both fail closed unless its action
list, observations, nonce digest, and trajectory digest bind exactly to that captured
trajectory. Production actions are explicitly marked with the Candidate service entry in
the trajectory, so native diagnostic tests cannot silently stand in for dispatcher,
browser-sync, or workspace-reconciler behavior. No deterministic checkpoint/inspect
sequence or evaluator-synthesized result artifact remains in the lower path.

Formal mode now uses two lower-broker lifetimes. The public lower broker serves
Builder dev feedback, is ownership-cleaned after the Candidate is frozen, and
is replaced by a fresh evaluator-owned `gpt-5.6-sol/medium` hidden broker. The
new broker's initial stats must prove zero calls and zero failures before the
controller endpoint and stats file are switched. Both broker container IDs are
cleaned and independently attested.

Provider-free validation run on September 6, 2026:

- `python3 -m py_compile harbor/formal_one_stop.py lower_agent/openhands_lower_agent.py`
- `python3 harbor/formal_one_stop.py --self-test`
- `python3 harbor/formal_one_stop.py --pilot-self-test`
- `python3 harbor/self_test.py`
- `python3 evaluator/harness/self_test.py`
- `python3 -m unittest -v evaluator.tests.test_0905_offline_contract`
- A local fake-broker run against an existing OpenHands candidate worktree
  exercised model-selected `create_checkpoint`, `acquire_recovery_lease`, and
  `start_production_sync` actions through the Candidate
  `ConversationService.createRecoverySyncCoordinator` factory. The resulting
  trajectory recorded the production service surface and the Python launcher
  accepted the model-artifact/trajectory binding. It was not provider smoke
  evidence and did not alter the historical runtime.

The sibling remains `REPAIR`. No paid-provider smoke, formal evaluation, shared
repair-file update, historical-run promotion, or formal score claim was made.
The lower action catalog now exposes the stable product-facing dispatcher,
browser-sync, and workspace-reconciler factory methods through bounded
`production_*` actions. A fresh current-digest provider smoke is still required
before any readiness promotion, and the sibling remains `REPAIR` until the
smoke demonstrates those actions with real medium-broker calls.

## 2026-09-07 formal-entry documentation correction

The current sibling documentation now consistently names
`harbor/formal_one_stop.py --run-formal` as the formal entry point. Provider-free
verification was rerun after this documentation-only correction:

```text
python3 -m py_compile harbor/formal_one_stop.py lower_agent/openhands_lower_agent.py
python3 harbor/self_test.py
python3 self_test.py
```

Both self-tests passed. This does not create current provider smoke evidence or
change the sibling's `REPAIR` status.

## 2026-09-07 Builder-turn boundedness repair

The latest current pilots showed a repeatable upper-lifecycle bottleneck rather
than a lower-agent failure. In `0907-smoke-current-014`, the Builder completed
46 successful `gpt-5.6-sol/xhigh` broker calls and edited a broad set of real
Candidate recovery surfaces, but the single Codex turn lost its broker stream
after five reconnect attempts before it submitted Candidate 1. The evaluator
therefore recorded zero accepted Candidates and zero lower calls; this is not
evidence that the lower product failed.

The current Builder instruction now explicitly requires the first submission to
be the smallest coherent product slice satisfying the visible public contract,
to submit immediately after public validation, and to defer optional hidden
recovery surfaces to the feedback revision. The pilot instruction carries the
same boundedness rule for `dev_001`. This changes only upper Builder pacing and
submission timing; it does not pre-execute lower actions, weaken hidden cases,
or select the Agent's product trajectory.

Focused provider-free verification remains required after this source change;
the previous current pilot remains historical non-publishable evidence and is
not promoted.

## 2026-09-08 Builder Compose address-pool repair

The `0908-smoke-current-001` run did not exercise the Builder or Candidate.
Harbor failed during `docker compose up` with `could not find an available,
non-overlapping IPv4 address pool among the defaults to assign to the network`.
The wrapper-level Builder exit record and zero submissions are therefore
infrastructure evidence, not Candidate behavior.

The current one-stop now selects a deterministic run-local `/24` from RFC 2544
benchmarking space, excludes every subnet already reported by Docker and every
parseable host route, and writes that subnet as the Compose project's explicit
default-network IPAM configuration. Harbor's default-network and egress-
sidecar topology remain unchanged; only address allocation is made explicit.

Provider-free verification now includes 10 offline contract tests, Python
compilation, both OpenHands self-tests, and CLI loading. A daemon-level probe
created, inspected, and removed the selected `198.18.17.0/24` bridge. One
stopped Compose sidecar and empty network were also removed using the exact
completed DeepTutor run working-directory label; an older empty network with
no sufficient run ownership mapping was left untouched. No provider call or
formal execution was started. OpenHands remains `REPAIR` until a current
accepted Candidate and the required two-hidden real smoke complete.

## 2026-09-09 current-016 provider-infrastructure pilot result

`0909-smoke-current-016` was the first live pilot after the Builder-turn,
Compose-network, and production-surface repairs. The Builder completed 72
successful `gpt-5.6-sol/xhigh` broker calls and produced valid Candidate
delivery/materialization/build attempts. The Candidate's real lower entry and
production ConversationService surface were reached, but the evaluator-owned
public lower broker returned HTTP 502 for all 9 `gpt-5.6-sol/medium` calls.

Because there was no successful provider response, the lifecycle correctly
classified the attempt as `provider_failure`, did not consume a Candidate
round, did not accept or freeze a Candidate, and did not start hidden or judge
execution. This is provider/evaluator infrastructure evidence, not a Candidate
failure and not readiness evidence. The run-owned Builder and public
containers were removed and inspected absent, the hidden container was never
started, cleanup reported `unrelated_containers_touched=false`, and no
run-owned compose network remained.

## 2026-09-09 lower Responses transport normalization

Both `0908-smoke-current-003` and `0909-smoke-current-016` reached the same
OpenHands product entry but observed 9/9 public lower failures. The candidate
surface received broker 502 responses while the evaluator broker's private
stats consistently recorded upstream HTTP 400. The local Agent Server emits
one text task, whereas the successful shared Responses paths use the canonical
message/input-text representation.

The evaluator-owned OpenHands broker now normalizes a non-empty text `input`
into one `user` message containing one `input_text` part and locks
`stream=false`. Already structured non-empty input remains unchanged. This is
transport normalization only: the broker still preserves the exact task text,
locks `gpt-5.6-sol/medium`, and does not choose, synthesize, or rewrite any
product action. Eleven focused offline tests, Python compilation, the formal
self-test, and the pilot self-test pass with zero provider and Docker calls.

`0909-smoke-current-017`, the first pilot after this repair, made 18 Builder
calls (17 completed successfully and one in flight at timeout) but reached the
1800-second Builder limit before Candidate delivery. Public lower calls
therefore remained zero, hidden execution did not start, and this run cannot
validate the transport change. Its owned broker containers were inspected
absent; its run-owned Compose main/sidecar containers and network were removed
by the exact project name without touching unrelated resources.

Two evaluator-owned transport probes then isolated the remaining rejection.
With canonical structured input and `stream=false` but the Agent Server's
non-standard top-level `metadata` envelope present, the upstream returned HTTP
400. The same request without `metadata` returned HTTP 200 with one successful
broker call. The broker now removes only that envelope field before forwarding;
the exact task text, locked model/effort, and model-selected product trajectory
remain unchanged. These probes are diagnostic only and do not count as
benchmark smoke or readiness evidence. A fresh current smoke is required.

## 2026-09-09 current-018 Builder timeout after transport repair

`0909-smoke-current-018` loaded the metadata-removal broker repair and
completed 63 successful Builder broker calls out of 64 recorded calls, with one
call in flight at the timeout boundary. The Builder authored a roughly 72 KiB
patch and repeated delivery attempts, but the lifecycle controller recorded no
Candidate submission before the 1800-second Builder limit. Consequently the
public lower broker remained at zero calls, no Candidate round was accepted or
frozen, hidden execution did not start, and the transport repair received no
benchmark lower-path validation in this run.

The run remains `pilot_builder_integration_incomplete`, not a Candidate or
provider lower failure. Owned Builder/public containers were removed and
inspected absent, the hidden container was never started, the run-owned Compose
network was removed, and cleanup did not touch unrelated containers.

## 2026-09-09 current-019 lower latency-boundary diagnosis

`0909-smoke-current-019` was the first run to traverse the repaired Builder,
Candidate lifecycle, and successful public lower transport together. The
Builder broker recorded 119 calls with 118 completed upstream 200 responses and
no failures. The lifecycle accepted two distinct Candidate digests and consumed
the first feedback digest before accepting Candidate 2. The public lower broker
recorded 9 successful `gpt-5.6-sol/medium` calls with zero failures.

Both `dev_001` attempts reached the Candidate's production
`ConversationService` surface and wrote a model-selected `trajectory.json`, but
the Vitest test was hard-coded to 120 seconds while the controller passed a
180-second case budget. The real Responses path used the available time on
five action responses before the test was killed, so `agent_result.json` was
never authored and the lifecycle correctly stopped in `feedback_ready`; hidden
cases and judge execution did not start. A provider-free fake-broker replay of
the same Candidate and action boundary continued through the artifact request,
isolating the observed stop to the latency-budget mismatch rather than a
provider HTTP error or a `closeSession` product deadlock.

The current source now gives the generated lower test a 360-second timeout and
raises the default controller case timeout to 360 seconds. The lower prompt also
states the product precondition that a checkpoint and current recovery lease
must be established before production dispatcher, sync, or workspace actions;
it does not prescribe an action trajectory. Provider-free compilation, the 11
offline contract tests, `harbor/self_test.py`, and `--pilot-self-test` pass after
this repair. A fresh current-digest smoke is required; current-019 is not
readiness evidence and does not authorize formal execution.

## 2026-09-09 current-020 post-repair lower timeout

`0909-smoke-current-020` completed 88 successful Builder broker calls with no
Builder broker failures and produced one accepted Candidate delivery. The
evaluator-owned public lower broker then completed eight successful
`gpt-5.6-sol/medium` calls with no provider or broker failures. The Candidate's
recovery adapter and production `ConversationService` were reached through a
real model-selected trajectory, which was persisted under
`lifecycle/round_001/dev_001/trajectory.json`.

The lower Vitest launcher nevertheless exited 124 at the evaluator-owned
360-second boundary before `agent_result.json` was authored. The lifecycle
therefore classified the attempt as `public_infrastructure_invalid`; no
Candidate round was consumed, no freeze occurred, and hidden cases plus the
independent judge did not start. This run proves that the transport and product
action loop are live, but it remains non-readiness pilot evidence and does not
authorize formal execution.
