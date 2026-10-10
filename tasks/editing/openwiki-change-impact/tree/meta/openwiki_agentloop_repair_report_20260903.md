# OpenWiki Agent-loop sibling repair report — 2026-09-03

## Scope

Only `@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree`
was modified. No authoritative source, `0830-edit-v2` staging, old formal run,
global repair manifest, or global progress file was changed. Work stopped on the
user's request before any long-running build, Builder session, or hidden suite.

## Implemented changes

- `agentloop/evaluator/fixture_service.py`: new evaluator-owned materializer for
  all public and hidden fixtures. It creates a fresh repository per invocation
  and rewrites directory-based before/after inputs to case-local paths.
- `agentloop/evaluator/controller.py`: records a stable Builder session id,
  Candidate 1 feedback digest, Candidate 2 parent/feedback binding, two public
  dev invocations per accepted Candidate, an evaluator-owned Candidate 2 frozen
  repository, and repository/candidate digests. Freeze rejects duplicate
  Candidates, missing dev feedback, infrastructure-invalid Candidate 2 dev
  evidence, and missing same-session revision metadata.
- `agentloop/evaluator/hidden_controller.py`: implements a real six-case
  post-freeze executor. It validates `source_submission=2`, timestamps,
  Candidate 1/2 distinction, feedback consumption, run-local frozen paths,
  exact inventory, and repository digest before and after every case. It writes
  `hidden-result.json` plus `hidden-after-freeze-attestation.json`, keeps
  per-case failures observable, stops on frozen-tree mutation, and rejects
  replay after `hidden_started_at` is set.
- `agentloop/evaluator/lower_agent_launcher.py`: invokes the compiled OpenWiki
  product through the evaluator-owned broker and fresh fixture workspace. It no
  longer synthesizes a successful `agent_result.json`; a missing product-owned
  terminal artifact is a Candidate contract/no-observable-behavior failure.
  Broker/provider failure and Candidate behavior classification are recorded
  separately.
- `agentloop/evaluator/dynamic_case_service.py`: adds case-local task text while
  withholding evaluator action plans, scenario metadata, expected pages,
  required/forbidden text, expected examples, and other oracle fields.
- `agentloop/evaluator/broker.py`: direct-script import bootstrap fixed so the
  sibling broker can launch from the benchmark root.
- `agentloop/dev_cases/run_public.py`: public lower-agent runner now
  materializes the real public fixture and passes it as the product workspace.
- `agentloop/protocol.py`: adds shared infrastructure classifications, UTC
  timestamp parsing, and SHA helpers.
- `schemas/freeze_manifest.schema.json`: expanded to require Candidate 1/2
  digests, repository digest, source submission id, Builder session id,
  feedback digest/consumption, freeze/hidden timestamps, dev completion, and
  the exact six-case inventory.
- `agentloop/evaluator/tests/test_agentloop_guards.py` and
  `agentloop/evaluator/tests/__init__.py`: new tests for request leakage,
  terminal artifact strictness, freeze digest/path validation, frozen-tree
  tamper detection, attestation, and provider-failure attribution.
- `agentloop/self_test.py`: public case directory counting now ignores cache
  directories instead of treating `__pycache__` as a third public case.

## Verification completed

Verification completed during this repair:

```text
python3 -m py_compile agentloop/protocol.py agentloop/evaluator/*.py \
  agentloop/evaluator/tests/*.py agentloop/dev_cases/run_public.py
result: exit 0

python3 -m unittest discover -s agentloop/evaluator/tests -v
result: 5 tests, all passed, 21.592 seconds. This full test run preceded the
final small replay/CLI-attribution patches; the final tree was subsequently
rechecked with `py_compile` but, per the stop request, the full test was not
restarted.

python3 agentloop/self_test.py
result: ok=true, phase=A, provider_calls=0, status=PARTIAL

python3 -m compileall -q agentloop dev_cases evaluator/harness evaluator/tests
result: exit 0
```

The fixture coverage probe materialized all eight inventories successfully:

```text
dev_001 git
dev_002 diff
test_001 git
test_002 directories
test_003 diff
test_004 git
test_005 directories
test_006 git
```

The copied native harness's full `python3 -m unittest discover -s
evaluator/tests -v` is not green: 22 tests ran with four failures and one
error. Current failures concern the pinned source digest, builder-visible
hidden-token scan, a 100-point durable control scoring 25, rubric-heading
parsing, and fixture frontmatter. Those existing native-harness consistency
issues were not repaired after the stop request and must not be represented as
passing Agent-loop evidence.

## Real execution evidence

A short public `dev_001` entry probe was run at:

```text
.runtime/openwiki-agentloop-probe-20260902/
```

Evidence files include:

```text
.runtime/openwiki-agentloop-probe-20260902/run/launcher_request.json
.runtime/openwiki-agentloop-probe-20260902/run/launcher_result.json
.runtime/openwiki-agentloop-probe-20260902/run/broker_before.json
.runtime/openwiki-agentloop-probe-20260902/run/broker_after.json
.runtime/openwiki-agentloop-probe-20260902/run/stderr.log
.runtime/openwiki-agentloop-probe-20260902/broker_stats.json
```

The probe did materialize the real public fixture and start the compiled
OpenWiki `dist/cli.js` product (`product_started=true`, exit `1`). It did not
reach the broker: calls/successes/failures were all zero and no
`agent_result.json` existed. The stderr records a missing `better-sqlite3`
native binding. Therefore this is only real launcher/product-entry evidence,
not a valid public dev run and not Agent-loop Result evidence.

There is no real Candidate 1 -> two public dev -> feedback -> same-session
Candidate 2 run, no actual Candidate 2 freeze, no post-freeze hidden execution,
and no real hidden attestation. Hidden Result and Code score remain `N/A`.

## Remaining blockers

1. Prepare a writable task-local OpenWiki runtime with a compatible Node 22
   `better-sqlite3` native binding. The current sibling `node_modules` was
   installed with scripts ignored and has no binding, so the product exits
   before any broker call.
   The current probe's launcher record says `candidate_no_observable_behavior`
   because broker calls were zero, while stderr proves the pre-broker native
   runtime failure. That pre-call dependency failure still needs an explicit
   `evaluator_failure`/runtime-infrastructure branch so it cannot be charged to
   Candidate behavior.
2. Run an actual Builder session that submits Candidate 1, receives the two-dev
   feedback digest, and produces Candidate 2 in the same session with the
   required revision metadata. The controller guards exist, but no real Builder
   session has exercised them.
3. Require the resulting Candidate to emit its own schema-valid
   `agent_result.json`. The existing candidate smoke tree does not provide this
   terminal Agent-loop artifact.
4. After Candidate 2 dev evidence is infrastructure-valid, create and verify the
   immutable freeze, then run all six evaluator-owned hidden cases through a
   credential-bearing evaluator broker and inspect the generated attestation.
5. Repair or reconcile the five current native-harness test failures without
   weakening hidden isolation or changing authoritative benchmark content.
6. Add a formal Result judge and independent Code judge only after the full
   real lifecycle exists. No score should be populated from the current static
   tests or entry probe.

## Process cleanup

The temporary broker started on port `18120` was terminated. A final process
check found no `agentloop/evaluator/broker.py` process using port `18120`.
