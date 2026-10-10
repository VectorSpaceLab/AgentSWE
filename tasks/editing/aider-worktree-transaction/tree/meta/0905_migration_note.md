# 0905 Edit repair migration note — Aider worktree transaction

Date: 2026-09-05

This sibling remains `REPAIR`; no paid provider call, Docker workload, or formal
hidden run was started during the repair.

The lower prompt now presents the operational goal and allowed transaction
interface without prescribing an exact recovery script. The one-session
Builder lifecycle accepts up to ten distinct valid submissions, evaluates both
dev cases after every accepted digest, treats duplicate digests idempotently,
does not freeze automatically on `dev mean > 60`, and runs all six hidden cases
only after freeze. Formal Result publication uses the shared independent
semantic judge. Formal Code publication uses the evaluator-owned wrapper around
the unchanged Create Code judge; the two axes are independent and
`combined_score` is null.

The lower execution path is the real patched Aider (`python -m aider`) using the
evaluator-owned broker and the candidate's `aider.worktree_plan_adapter` through
the Aider shell/tool loop. Hidden case manifests expose only a scenario and
bounded action interface; expected terminal answers remain evaluator-side.
Broker/provider failures, zero successful broker calls, and lower timeouts are
classified as `infrastructure-invalid`, while a native diagnostic score is
explicitly non-publishable and is not used as formal Result evidence.

The lower runner now records that `agent_result.json` came from the lower
product workspace and was copied only after the lower process exited. Formal
Result finalization validates that provenance and invokes the independent
shared Result judge; Code finalization invokes the independent Code judge and
does not consume the native diagnostic score. Formal startup tracks attempted
owned brokers, and cleanup attestation records inspect-after-remove absence plus
controller-socket closure even when startup fails partway through.

Provider-free checks completed: `infra/self_test.py`, formal static
`--self-test`, pilot `--pilot-self-test`, and targeted `py_compile` all passed.
`evaluator/harness/self_test.py` could not start because the system Python has
`jsonschema` 3.2.0 without `Draft202012Validator`; no package was installed or
network/provider/Docker workload started. A real evaluator-owned hidden smoke
and cleanup evidence are still required before readiness can become `READY`.

## September 6, 2026 prompt-naturalness repair

The six hidden prompts were rewritten around the user goal, observed repository
state, ownership constraints, required evidence, and honest failure reporting.
They no longer prescribe a complete recovery command order or tell the lower
Agent the exact terminal answer. The transaction vocabulary and safety
requirements remain available where needed for the real Aider interface, while
the dynamic conflict, lease, object, ref, and corruption facts remain
evaluator-controlled. This keeps the lower Aider shell/tool loop responsible
for choosing the operation sequence.

The sibling-local prompt metadata now records no confirmed prompt leakage;
hidden inventory, oracle, judge prompts, score weights, and credentials remain
evaluator-only. The change is static and does not claim a paid smoke or formal
readiness.

## September 6, 2026 evaluator-boundary repair

The real lower runner now reloads the evaluator-owned `CaseRuntime` state after
the Aider process exits before materializing `result.json`, `native_evidence.json`,
or `oracle_comparison.json`. Missing or malformed state is classified as an
evaluator infrastructure failure rather than raising an unrecorded
`NameError`. Formal mode also rejects a non-empty run directory without deleting
its contents. Focused provider-free regressions cover both behaviors.

This repair remains static. The current bounded real pilot still has no accepted
Candidate or hidden execution, so no lower-model score or formal readiness is
claimed.

## September 7, 2026 explicit shell-confirmation boundary repair

The latest current pilot reached the real Aider lower product and made a
successful medium-broker request, but the first model-selected action was not
executed. Aider's product code marks shell execution prompts as
`explicit_yes_required`; consequently `--yes-always` deliberately resolves
that prompt to `no`, and the non-interactive container's EOF produced the same
outcome. The persisted trajectory showed the exact `inspect` command followed
by `Run shell command? ... n`, with no product action or terminal artifact.

The evaluator lower runner now supplies a bounded 512-line `y` input stream
solely for Aider's confirmation prompts. It does not generate, alter, or
select shell commands; the model still chooses each command and Aider still
executes it through its own shell/tool loop. The case result records the
confirmation boundary, budget, and `model_selected_commands_unchanged` marker
for auditability. The budget covers command execution, output-addition, and
new-file confirmations needed for the product-authored artifact.

Provider-free verification passed:

```text
python3 -m py_compile evaluator/harness/run_lower_agent_case.py evaluator/harness/test_0906_contract.py
python3 -m unittest -v evaluator.harness.test_0906_contract
Ran 19 tests
OK
python3 harbor/formal_one_stop.py --self-test --run-dir /tmp/aider-confirm-repair-selftest
```

The separate `evaluator/harness/self_test.py` remains blocked by the host
`jsonschema` package lacking `Draft202012Validator`; this environment issue is
not recorded as a production pass. No provider-backed smoke or formal
evaluation was started, and a fresh current-digest smoke remains required.

## September 7, 2026 confirmation-repair pilot result

A fresh bounded pilot using the current sibling was run at
`@@AGENTSWE_EDITING_RUNS@@/smoke/aider/0907-smoke-current-016` after the
explicit-confirmation repair. The Builder used one evaluator-owned
`gpt-5.6-sol/xhigh` session and reached a ready Candidate preflight, but the
900-second Builder wall-clock limit expired while the first submission was
being delivered. No Candidate was accepted, no freeze or hidden execution
occurred, and no Result or Code judge was started. The public lower broker
recorded two calls with one upstream failure; this is integration/provider
evidence, not a scoreable lower result.

Cleanup completed: the exact current-run Builder and public containers were
removed and inspected absent, the controller socket closed, and no hidden
container was started. The run remains non-publishable and the sibling remains
`REPAIR` pending a fresh successful current-digest lifecycle.

## September 8, 2026 explicit-confirmation launcher repair

The current-017 smoke also exposed that the lower Aider launcher still passed
`--yes-always` while supplying affirmative stdin. In Aider, that flag resolves
prompts marked `explicit_yes_required`—including model-proposed shell commands—
to `no`; therefore the model-selected `inspect` command was displayed but not
executed. Both attempted public executions had no successful product action,
no product-authored artifact, and no valid Candidate evidence.

The evaluator-owned launcher now removes `--yes-always` and retains a bounded
affirmative stdin stream (`y\\n` x 512). This crosses Aider's product
confirmation boundary without selecting or rewriting the command chosen by the
lower model. The contract regression suite now asserts that the flag is absent
and that the evaluator-owned confirmation input is present; 19 focused tests
pass. The changed-file inventory includes
`evaluator/harness/run_lower_agent_case.py` and
`evaluator/harness/test_0906_contract.py`.

The old current-017 run remains invalid because it started before this launcher
repair and recorded no action/artifact evidence. A fresh current-018 smoke is
required; this sibling remains `REPAIR` until a new run demonstrates a real
broker call, model-selected product action, lower-workspace artifact
authorship, and the required post-freeze hidden evidence.

## September 8, 2026 current-018 pilot result

The fresh current-018 pilot reached the evaluator-owned Builder broker and
completed 63 successful upstream calls (64 calls total, with one in flight at
the timeout snapshot), with zero provider, protocol, or delivery failures.
However, the single Builder session hit the 900-second wall-clock limit with
exit code 124 before submitting Candidate 1. The session attestation records
zero accepted submissions, no dev execution, no freeze, and no feedback
digest. The public lower broker recorded zero calls, and hidden execution was
not started. Accordingly the run status is
`pilot_builder_integration_incomplete`, not a Candidate failure and not
readiness evidence.

Cleanup was complete: the run-owned Builder and public containers were removed
and inspected absent, the controller socket was closed, and no hidden
container was attempted. The sibling remains `REPAIR`; a later fresh smoke is
needed after the Builder delivery lifecycle is made to complete within the
bounded pilot.

## September 8, 2026 Builder diagnostic-budget repair

The current-018 Builder trajectory showed that the Builder spent its bounded
session repeatedly running the public fixture through an evaluator-only Python
environment that is not mounted in the Builder container. It then tried local
package installation and temporary import shims before repeating fixture
diagnostics. This consumed the 900-second session without reaching delivery.

The evaluator-owned Builder instruction now states that this environment is not
available in the Builder container, treats optional local dependency failures as
non-blocking, and bounds fixture diagnostics to one final check. It does not
prescribe the adapter's recovery sequence or alter the lower product path. The
new contract regression and compile checks pass (20 focused tests total).

## September 8, 2026 current-019 infrastructure result

The next fresh smoke was started at
`@@AGENTSWE_EDITING_RUNS@@/smoke/aider/0908-smoke-current-019`, but Docker
could not create the Harbor Builder compose network: the daemon reported that
no non-overlapping IPv4 address pool remained. The failure occurred before the
Builder container started, so Builder, public lower, and hidden brokers all
recorded zero calls. This is `pilot_infrastructure_invalid`, not a Candidate
failure and not readiness evidence. Cleanup verified the attempted run-owned
Builder/public resources absent and closed the controller socket; no hidden
container was attempted. Existing unrelated Docker networks and containers
were left untouched.

## September 8, 2026 lower continuation repair

The next completed lifecycle exposed a separate product-loop boundary. Aider's
`--message-file` mode sends one user message, processes one model reply, and
then exits. When that reply selects a shell command, Aider executes the command
and appends its output to chat history, but it does not automatically request a
second model reply. The previous lower launcher therefore stopped after the
first successful `inspect` action even though the operational task required
additional model-selected actions and a product-authored artifact.

The lower runner now permits at most 12 bounded Aider turns in one lower
workspace. Turn 1 uses the original task message. If the artifact does not yet
exist, subsequent turns use `--restore-chat-history` with a generic
continuation message directing the model to read the latest command output,
avoid repeating a successful action, choose the next justified action, and
write the artifact through Aider's edit loop once evidence is sufficient. All
turns share the same candidate, product workspace, case runtime, chat history,
input history, and evaluator-owned broker. The evaluator does not select,
generate, or rewrite the next product action. The result records turn count,
the turn limit, and the number of restored-history continuations in its
confirmation-boundary evidence.

## September 8, 2026 current-020 through current-022 pilot results

`0908-smoke-current-020` made 38 Builder broker calls, of which 37 completed
successfully, and reached a passing focused adapter test. It nevertheless hit
the 900-second Builder timeout before creating a delivery, so no Candidate was
accepted and no lower or hidden execution began.

`0908-smoke-current-021` used an 1800-second Builder budget and completed the
two-submission feedback lifecycle: two distinct Candidate digests were
accepted, the feedback digest was consumed, and Candidate 2 was frozen. Its
two public lower attempts, however, were launched with the older single-turn
boundary and each stopped after one successful model-selected `inspect` action.
They produced no `agent_result.json`; hidden and judge execution therefore did
not start. This is an evaluator-launcher failure, not evidence that either
Candidate failed the requested product behavior.

`0908-smoke-current-022` began before the final continuation patch was active.
It accepted Candidate 1 after 74 successful Builder calls. The public lower
broker completed 13 calls, but the old continuation behavior repeated
`inspect` for all 12 bounded Aider turns and produced no artifact. The Builder
then exited during the Candidate 2 submission phase; the controller froze the
last accepted Candidate, did not start hidden execution, and published no
Result or Code score. Its run-owned Builder containers and compose network were
removed after the controller finished; unrelated Docker resources were not
touched.

These pilots are non-publishable. A fresh current-digest smoke started after
the bounded continuation repair is still required to demonstrate a
model-selected follow-up action, lower-workspace artifact authorship,
post-exit artifact copying, valid public classification, and post-freeze hidden
execution before this sibling can leave `REPAIR`.

## September 9, 2026 current-023 continuation-repair pilot result

`0908-smoke-current-023` was the first pilot launched after the bounded
multi-turn lower launcher repair. Docker startup succeeded and the Builder
broker completed 7 successful calls out of 8 recorded calls, with one call in
flight at the timeout snapshot. The Builder nevertheless reached its 1800
second wall-clock limit before Candidate delivery. Its public and hidden lower
brokers recorded zero calls, no Candidate was accepted, no artifact or
product-authored lower evidence was produced, and no hidden or judge stage was
started.

The run's owned Builder and public containers were inspected absent after
cleanup, the hidden container was never attempted, the controller socket was
closed, and the run-owned compose network was removed after confirming it had
no endpoints. This is `pilot_builder_integration_incomplete`, not evidence
against the continuation implementation and not readiness evidence. Static
checks remain green, but a later fresh run must first complete the Builder
delivery before the repaired lower product loop can be evaluated.
