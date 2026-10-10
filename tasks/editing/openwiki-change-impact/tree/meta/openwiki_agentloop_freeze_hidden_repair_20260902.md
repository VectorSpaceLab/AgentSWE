# OpenWiki immutable freeze and post-freeze hidden repair — 2026-09-02

## Scope and outcome

This change modified only the sibling at:

```text
@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree
```

The authoritative source, `@@AGENTSWE_LEGACY_HARBOR@@/0830-edit-v2`, old runs, other
siblings, and global progress/manifest documents were treated as read-only.
No expensive Builder session, provider-backed formal run, six-case formal
hidden run, Result judge, or Code judge was started.

The sibling now has a fail-closed Candidate 2 freeze and a real evaluator-owned
post-freeze hidden controller. Static tests and local-only broker smoke pass.
Formal Result and Code score remain `N/A` until a complete real lifecycle is
executed.

## Implemented invariants

### Candidate 2 freeze

- Freeze requires exactly two Candidate records and distinct Candidate delivery
  digests.
- Both Candidate 1 and Candidate 2 must have the canonical public inventory
  `dev_001`, `dev_002`, real feedback, and no infrastructure-invalid public
  evidence.
- Candidate 2 must be valid, same-Builder-session, and bound to the Candidate 1
  feedback digest.
- The freeze manifest requires `source_submission=2`, a nonempty source
  submission id, Candidate 1/2 digests, `candidate_digest`, and the mandatory
  `repository_digest` using `sha256-tree-v1`.
- The Candidate 2 repository is copied to the evaluator run directory. External
  symlinks are materialized; remaining symlinks must resolve inside the frozen
  repository.
- All write bits are removed from the frozen repository. The manifest and its
  SHA-256 seal are also made read-only.
- Re-freezing an existing run is rejected.

### One-time hidden gate

- Hidden requires a sealed, read-only, run-local Candidate 2 repository whose
  current digest matches both digest fields in the immutable freeze manifest.
- `hidden_started_at` remains `null` in the freeze manifest. Post-freeze state
  is not allowed to rewrite the freeze evidence.
- The controller atomically creates `hidden-once-gate.json` with
  `O_CREAT|O_EXCL`. A crash still consumes the gate, and replay requires a new
  freeze.
- The output must be the run-local `hidden-result.json`; an existing result is
  rejected.
- The only accepted inventory is the ordered canonical sequence
  `test_001` through `test_006`.

### Immutable repository and per-case evidence

- The frozen repository digest is measured before and after every case and
  recorded as both `frozen_digest_*` and `repository_digest_*`.
- The frozen tree is never the writable OpenWiki work directory. Each case gets
  disposable writable product/workspace copies; this permits normal OpenWiki
  state writes while preserving the immutable Candidate source.
- A changed frozen digest is classified as `mount_isolation_failure`; execution
  stops and the suite is infrastructure-invalid.
- Every attempted case writes `hidden-case-attestation.json` and
  `evidence-manifest.json`. The evidence manifest records path, existence,
  byte size, and SHA-256 for launcher request/result, broker before/after,
  trajectory, product-authored `agent_result.json`, Candidate request, and case
  attestation.
- The suite writes `hidden-after-freeze-attestation.json` with ordered executed
  inventory, repository digest stability, broker identity/stats, per-case
  classifications, evidence paths, and formal eligibility.
- Candidate behavior failures are measured outcomes. They do not make the
  infrastructure invalid. Missing cases, digest mutation, provider/broker/
  credential/launcher/evaluator failures, or a non-fresh broker keep the Result
  axis at `N/A`.

### Evaluator-owned broker and credential isolation

- A fresh broker process is created for one hidden suite and stopped after it.
  It has a unique `broker_instance_id`; initial stats must show zero calls.
- The broker binds only to `127.0.0.1`, forces `gpt-5.6-sol` with reasoning
  effort `medium`, and accepts only the Candidate placeholder bearer token.
- The evaluator credential file supports dotenv forms of `OPENAI_API_KEY` and
  `GATEWAY_API_KEY`, including quoted values and `export`. `GATEWAY_API_KEY` is mapped
  to the upstream bearer credential inside the evaluator broker.
- The real value is not copied into the broker environment, Candidate
  environment, command line metadata, stats, lifecycle record, or evidence.
  Only the non-secret source-key name is recorded.
- Candidate execution receives a minimal environment. It sees
  `OPENAI_API_KEY=broker-only-placeholder` and the evaluator broker URL; it does
  not inherit `GATEWAY_API_KEY`, a real `OPENAI_API_KEY`, or unrelated provider
  credentials from the evaluator host.
- Broker stats separately count `broker_failures`, `provider_failures`, and
  `credential_failures`, in addition to calls, successes, token usage, and
  forced overrides.

### Failure attribution

The lower launcher and hidden controller preserve independent infrastructure
and Candidate attribution:

| Family | Representative classification | Result-axis treatment |
|---|---|---|
| Broker protocol/stats/decoding | `broker_failure` | infrastructure-invalid, `N/A` |
| Upstream transport/HTTP | `provider_failure` | infrastructure-invalid, `N/A` |
| Missing evaluator credential | `credential_mount_failure` | infrastructure-invalid, `N/A` |
| Process spawn/native runtime | `launcher_failure` | infrastructure-invalid, `N/A` |
| Evaluator/fixture exception | `evaluator_failure` | infrastructure-invalid, `N/A` |
| Frozen-tree mutation | `mount_isolation_failure` | infrastructure-invalid, `N/A` |
| Missing product artifact | `candidate_contract_failure` | measured Candidate outcome |
| No model/tool activity | `candidate_no_observable_behavior` | measured Candidate outcome |
| Product nonzero/timeout | `candidate_product_failure` / `candidate_timeout` | measured Candidate outcome |
| Valid product result | `candidate_product_success` | measured Candidate outcome |

The CLI exits zero only when the six-case inventory is complete and
`formal_result_eligible=true`. A complete inventory containing an
infrastructure failure therefore cannot look successful to an outer runner.

## Files changed

- `agentloop/protocol.py`
  - immutable-tree, writable-runtime-copy, and symlink helpers.
- `agentloop/evaluator/controller.py`
  - canonical two-round dev gates, Candidate 2 freeze/seal, and credential-owned
    hidden invocation.
- `agentloop/evaluator/hidden_controller.py`
  - atomic one-use gate, canonical six-case execution, per-case digests/evidence,
    suite attestation, and independent broker lifecycle.
- `agentloop/evaluator/broker.py`
  - GATEWAY/OpenAI dotenv resolution, redacted stats, forced protocol, failure
    counters, and owned process lifecycle.
- `agentloop/evaluator/lower_agent_launcher.py`
  - broker v2/instance validation, credential-free Candidate environment,
    writable disposable copies, and split failure attribution.
- `agentloop/evaluator/tests/test_agentloop_guards.py`
  - freeze, replay, evidence, credential, broker, provider, launcher, and
    Candidate behavior regression tests.
- `agentloop/self_test.py`
  - static checks for immutable freeze, atomic gate, evidence, broker identity,
    GATEWAY mapping, and credential redaction.
- `schemas/freeze_manifest.schema.json`
- `schemas/broker_stats.schema.json`
- `schemas/infra_classification.json`
- `README-agentloop.md`

## Verification evidence

Final targeted verification:

```text
python3 -m unittest agentloop.evaluator.tests.test_agentloop_guards -v
Ran 15 tests in 56.518s
OK
```

The 15 tests cover:

- real `Controller.freeze()` Candidate 2 source submission, digest, seal,
  read-only tree, and duplicate-freeze rejection;
- Candidate 1 infrastructure-invalid feedback rejection;
- freeze manifest digest/path/seal validation;
- atomic one-use hidden gate and replay rejection;
- exact six-case inventory, per-case repository digests, evidence manifests,
  and Candidate-failure eligibility;
- tamper stop and partial-inventory attestation;
- provider failure remaining infrastructure-invalid;
- missing evaluator credential becoming `credential_mount_failure` and
  consuming the gate;
- process spawn failure becoming `launcher_failure`, not Candidate failure;
- separate broker/provider/credential counter classification;
- missing product terminal artifact rejection;
- GATEWAY dotenv mapping and Candidate environment secret removal;
- real local broker process startup/health/stop with no secret persisted;
- local-only fake-upstream forwarding, forced model/effort, token accounting,
  and secret redaction. This smoke made no external network or model call.

Additional checks:

```text
python3 agentloop/self_test.py
ok=true, phase=A, provider_calls=0, status=PARTIAL

python3 -m py_compile agentloop/*.py agentloop/evaluator/*.py \
  agentloop/evaluator/tests/*.py
exit 0

python3 -m compileall -q agentloop dev_cases evaluator
exit 0

python3 agentloop/evaluator/controller.py --self-test ...
controller=loadable

python3 agentloop/evaluator/hidden_controller.py --help
exit 0

python3 agentloop/evaluator/broker.py --help
exit 0

JSON parse of the three changed schemas
SCHEMAS_JSON=PASS
```

## Existing native-harness status

The copied native harness is not represented as green. A diagnostic full run
of `python3 -m unittest discover -s evaluator/tests -p 'test_*.py' -v` executed
22 tests and observed four failures plus one error. The failures concern the
already-diverged pinned `input/repository` digest, a builder-visible token scan,
the native 100-point control/rubric-heading expectation, and fixture
frontmatter. This repair did not change authoritative/native case content to
make those tests pass. They remain a separate sibling baseline inconsistency,
not evidence for or against the new Agent-loop freeze/hidden guards.

## Formal status and next runtime gate

No formal score is claimed:

```text
formal Builder lifecycle = not started in this repair
real post-freeze hidden   = not started in this repair
Agent-loop Result         = N/A
Code score                = N/A
formal aggregation        = not started
```

A future real run must supply an evaluator-owned credential file and the GATEWAY
upstream without mounting either into Candidate-visible paths. It must then
produce a valid same-session Candidate 1 → two dev → feedback → Candidate 2 →
two dev lifecycle before this controller will freeze and consume the one-time
hidden gate. A compatible task-local Node/OpenWiki native runtime remains a
runtime prerequisite; native dependency failure is classified as launcher
infrastructure and cannot be charged to Candidate behavior.
