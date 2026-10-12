# 0905 Edit repair migration note — AI Scientist reproducibility gate

Date: 2026-09-04

This P0 sibling remains `REPAIR`. No publishable formal Builder or six-hidden
evaluation has been accepted during the repair.

Complete lower-runtime tasks now exist for all six hidden cases. The lifecycle
supports one through ten accepted Candidate submissions in one Builder session,
evaluates both dev cases after each accepted digest, requires fresh feedback for
every accepted submission, does not freeze automatically on `dev mean > 60`,
and keeps every hidden run post-freeze. The
semantic finalizer uses the shared independent Result judge and routes Code
through the evaluator-owned wrapper around the unchanged Create judge. The
native scientific harness remains an evidence/oracle mechanism, not a semantic
Result scorer.

The complete provider-free evaluator suite passed. The historical September 4,
2026 local-time non-formal smoke exercised `test_001` and `test_006`
through the governed product entry with successful `gpt-5.6-sol/medium` broker
calls. Each case produced a model-authored artifact and native/oracle-isolated
evidence, then received exactly one completed `gpt-5.6-sol/xhigh` semantic
Result judgment inside a short-lived evaluator-owned judge container. All
owned containers were attested absent afterward. The evidence bundle is under
`@@AGENTSWE_EDITING_RUNS@@/smoke/ai-scientist/0905-smoke-reference-001/`.

The fresh September 5, 2026 attempts are retained separately and are not
upgraded to publishable evidence. `0905-smoke-reference-003` reached a frozen
Candidate and all six real lower executions, including genuine model-authored
artifacts for successful cases, but provider infrastructure failures made the
hidden evidence non-publishable and the independent Result judge was not
invoked. `0905-smoke-reference-004` ended before any accepted Candidate after
Builder/provider delivery failures. Both runs completed evaluator-owned
cleanup, and neither is a Candidate-zero result.

The current launcher now keeps evaluator-owned provenance in
`launcher_result.json`, classifies final-authoring transport failures as
infrastructure, and only the complete hidden inventory reaches the finalizer;
the finalizer itself preserves N/A for infrastructure cases. The provider-free
suite was rerun after these changes with all 20 evaluator tests passing, plus
the agentloop self-test and static one-stop self-test.

## 2026-09-06 lower-agent semantic-gap repair

The earlier lower launcher asked the evaluator broker for a rationale, then
always dispatched `launch_scientist_bfts.py --verify-claims-only` with
`--claim-operation verify`. That was not a model-selected product trajectory.
The current repair replaces that path with a bounded loop of at most eight
model decisions. Each decision must be strict JSON selecting exactly one of
`prepare`, `status`, `commit`, `verify`, or `cancel`, or an explicit `finish`
after at least one action. The evaluator dispatches only the selected operation
through the Candidate governed launcher and returns sanitized stdout/stderr,
receipt projections, receipt digests, release hashes, and exit status before
the next decision. Unsupported operations, malformed decisions, empty
trajectories, and action-budget exhaustion are Candidate behavior failures;
broker/provider transport failures remain infrastructure-invalid.

Every run now writes evaluator-owned `raw_action_trajectory.json`. The lower
model's final `agent_result.json` is still accepted only when it is authored by
the broker response and its nonempty `tool_events`, operation list, per-step
receipt digests, trajectory digest, runtime digest, and final release hashes
match the observed trajectory. The independent AI Scientist finalizer repeats
these checks and rejects legacy empty or mismatched artifacts. No semantic
fields are synthesized by the evaluator, and rejected model artifacts are
retained only as explicitly rejected evidence.

Provider-free coverage added in
`evaluator/tests/test_0910_lower_action_loop.py` verifies selected-operation
dispatch rather than fixed `verify`, empty-trajectory rejection, mismatched
tool-event rejection, unsupported-operation rejection, and provider-failure
classification. The existing offline provenance contract fixture was upgraded
to include a valid nonempty raw trajectory. On September 6, 2026, the full
provider-free suite passed (26 tests), the agent-loop self-test passed, the
one-stop self-test passed, and compilation passed. No paid-provider smoke,
Docker smoke, or formal evaluation was run by this repair worker; the sibling
therefore remains `REPAIR` pending fresh real evidence.

## 2026-09-07 formal-entry correctness repair

The current formal controller passes each lower case's
`agentloop/cases/<case>/case_input.json` into the lower launcher. Those current
case directories contain `task.md`, not the legacy `input.md` name. The lower
launcher now reads `task.md` first and uses `input.md` only for older
provider-free fixtures, preventing a deterministic pre-model file-not-found
failure on every formal hidden case.

The hidden-after-freeze attestation previously wrote
`all_cases_started_after_freeze: true` without checking the recorded times.
It now parses the freeze timestamp, hidden-phase start, and every case's
`started_at`, and reports true only when all are valid and at or after the
freeze. Provider-free tests cover both the valid and pre-freeze cases.

This correction changes the source digest and invalidates any prior smoke
manifest that was not produced from the corrected digest. No formal or paid
evaluation was started by this repair; a fresh current-digest smoke remains
required.

## 2026-09-07 canonical task-digest binding repair

The lower launcher now resolves the canonical `agentloop/cases/<case>/task.md`
before emitting runtime or launcher evidence and records both its source label
and SHA-256 digest. The semantic finalizer verifies that the launcher evidence,
the run-local runtime fixture, and the current canonical task file agree before
it accepts an artifact or invokes the independent Result judge. The judge now
receives a verified run-local copy of that canonical task, rather than the
legacy `test_cases/<case>/input.md` path. A regression mutates the canonical
task after launcher evidence is recorded and confirms finalization rejects the
mismatch.

Focused verification passed:

```text
python3 -m py_compile agentloop/lower_agent_launcher.py evaluator/semantic_finalize.py evaluator/tests/test_0905_offline_contract.py
python3 -m unittest evaluator.tests.test_0905_offline_contract
Ran 9 tests
OK
```

This is a provider-free correctness repair. It does not promote historical
evidence or claim a current smoke; a fresh current-digest lower-agent smoke is
still required.

## 2026-09-08 CaseWorld semantic-wiring hardening

The queued P0 follow-up found that the earlier CaseWorld migration was not
actually executable in the current tree: `lower_agent_launcher.py` still called
the removed `CaseWorld.apply_next()` method. It also found that the old
provider-free fixtures treated every product command as a successful release,
which could not prove that evaluator transitions were tied to substantive
product state.

The sibling-local repair now:

- connects hidden execution as `prepare_action → model-selected product action
  → optional evaluator-owned probe → observe_action`;
- advances a transition only when its operation-specific product receipts,
  durable state, mutation proof, and required probe evidence pass;
- leaves unrelated operations and failed/no-op product observations as recorded
  attempts without advancing CaseWorld;
- snapshots the concurrent winner context before replacing the mutable context,
  preventing a successful race from erasing the context used by later product
  arguments;
- passes `takeover_generation` through the real `--claim-takeover-generation`
  product argument when a recovery transition requires it;
- keeps CaseWorld disabled for public `dev_001`/`dev_002` dry-run compatibility,
  while retaining the full hidden-only sequence gate;
- changes provider-free product fixtures to model prepared/reserved,
  committed/settled, cancellation, integrity rejection, policy conflict,
  generation takeover, shared-budget contention, and idempotent notification
  state instead of accepting every command unconditionally;
- adds negative coverage for unrelated actions, failed/label-only evidence,
  and missing takeover propagation.

Verification performed without providers or Docker:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evaluator/tests -p 'test*.py'
Ran 35 tests in 14.538s — OK

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  evaluator.tests.test_0910_lower_action_loop \
  evaluator.tests.test_0911_case_world_contract \
  evaluator.tests.test_0905_offline_contract
Ran 22 tests in 0.254s — OK

PYTHONDONTWRITEBYTECODE=1 python3 agentloop/self_test.py
self-test: PASS

PYTHONDONTWRITEBYTECODE=1 python3 harbor/formal_one_stop.py \
  --run-dir <fresh-temporary-directory> --self-test
self_test: PASS; provider_calls=0; docker_started=false; harbor_started=false

PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
  agentloop/case_world.py agentloop/lower_agent_launcher.py \
  evaluator/semantic_finalize.py agentloop/two_round_controller.py \
  agentloop/run_hidden.py harbor/formal_one_stop.py
exit code 0
```

This remains a static/provider-free repair. It does not promote the existing
smoke bundles, establish a current-digest real hidden smoke, or open the
central 10/10 formal-readiness gate.
