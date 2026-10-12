# DeepTutor Agent-loop delivery report — 2026-09-03

## Scope and write boundary

This report covers only:

`@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/tree`

The authoritative DeepTutor source, the 0830 staging tree, old formal runs,
and the shared/global reports were not modified. The following worktree was
read-only migration input:

`@@AGENTSWE_LEGACY_DATA@@/0902-edit-agentloop-pilot/deeptutor/run-20260902-builder-session-v2/candidate2-work`

Its recorded Git commit is:

`853e0bfa437b81aaa167dacc30f5b2a6340d04db`

That worktree contains uncommitted tracked and untracked changes. It is useful
as a verified capability migration source, but it is not a real Builder
conversation and is not being represented as formal Candidate 2 provenance.

## Delivery produced

The sibling `delivery/solution.patch` was regenerated against the pinned
authoritative product tree at:

`@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/a0src/input/repository`

The delivery is repository-relative, non-empty, and contains exactly these six
product paths:

1. `deeptutor/api/routers/mastery_path.py`
2. `deeptutor/capabilities/mastery/tools.py`
3. `deeptutor/learning/adaptive.py`
4. `deeptutor/learning/models.py`
5. `deeptutor/learning/tests/test_snapshot_chain_audit.py`
6. `deeptutor/tools/mastery_tool.py`

The migrated product capability is the read-only
`mastery_learner_snapshot_chain_audit` surface. It recomputes the complete
persisted witness chain, checks path identity, sequence continuity, duplicate
bindings, previous-root links, leaf digests, root digests, inclusion proofs,
consistency proofs, and optional expected-head fences. It returns bounded
summaries while auditing the full chain and does not repair, advance, or persist
state. The existing delivery also retains the adaptive-remediation surfaces
from the prior sibling patch.

Recorded digests:

| Artifact | SHA-256 |
|---|---|
| `delivery/solution.patch` | `64c4dcc321699f5595d121fdbac788b23adf57b00bc4ee7445ec9bb095eba500` |
| `delivery/` recursive digest | `a28f7400561fe7f5b3c447948bffe9812449e0269a4e6f074354691c81213def` |
| materialized patched repository | `1c0e87a302a280df13404507102fff3e88f3846bbb506a9ac1823425cf8ac4b8` |

The authoritative staged product digest used by the adapter is
`56d107ba0798b0c2f0f852db4af6b8860c75b08f2f43e8d2971ad15e8202c862`.

The corresponding evidence is under:

`.pilot-runtime-final-evidence/stage-a-20260903/`

## Runtime and lifecycle changes in this sibling

The sibling-only harness now has the following gates:

- `runtime_probe.py` inventories the task-local interpreter and required
  imports, checks Python 3.11–3.13 and Pydantic v2, and checks that the chain
  audit is registered. Dependency failures remain infrastructure failures;
  missing product capability remains a Candidate capability gap.
- `candidate_adapter.py` can run the runtime probe after patch materialization
  and preserves the distinction between a ready product, a runtime blocker,
  and a Candidate capability gap.
- `two_round_controller.py` requires an evaluator-attested Builder witness,
  fixed Builder model/effort, one continuous connection, two terminal public
  dev records for each consuming round, exact feedback-digest consumption by
  Candidate 2, and distinct Candidate digests.
- Candidate 2 freeze is an atomic private copy with no symlinks or special
  files. The controller verifies the digest before and after copying and
  records the frozen tree as read-only.
- `run_hidden.py` refuses hidden execution without a Candidate 2 freeze,
  verified feedback consumption, verified same-session provenance, a regular
  read-only frozen tree, a strictly later hidden start time, and a fresh
  zero-call evaluator broker locked to `gpt-5.6-sol` / `medium`. It runs
  exactly `test_001` through `test_006`, creates fresh writable case copies,
  records per-case broker deltas, and writes an attestation without publishing
  a Result or Code score.

These gates make a future clean formal run fail closed. They do not convert
the read-only migration input into evidence of a real Builder session.

## Verification performed

All checks below used the task-local interpreter:

`@@AGENTSWE_ENVS@@/deeptutor-task-env-v1/venv/bin/python`

Observed results:

- `python -m compileall -q agentloop tests`: PASS
- `python agentloop/self_test.py`: PASS
- `python -m unittest -v tests/test_agentloop_runtime.py`: PASS, 3/3
- `python -m unittest -v deeptutor.learning.tests.test_snapshot_chain_audit`: PASS, 2/2
- `python -m agentloop.candidate_adapter ... --runtime-python <task-local Python>`:
  PASS; patch check/apply and targeted product compilation passed
- `python -m agentloop.runtime_probe ...`: PASS; `runtime_ready`, 26 mastery
  tools observed, chain-audit tool registered
- delivery validation: PASS; no delivery errors

The first attempted capability test used an absolute filesystem path as a
`unittest` module name and failed at test-loader argument parsing. The same
test was then run correctly by module name and directly as a file; both runs
passed 2/2. This loader mistake is not product or harness evidence.

No model broker was contacted by this task. No Docker lower-agent execution,
public dev execution, hidden execution, formal Result evaluation, independent
Code judge, or aggregation was started.

## Remaining formal evidence

The sibling is improved and Stage-A verified, but remains `PARTIAL`. The
following evidence is still absent and must be generated later by the outer
evaluation:

1. One real Builder session using the locked Builder model/effort, with
   Candidate 1 submitted, both public dev cases executed, evaluator feedback
   returned, and Candidate 2 produced by that same session.
2. A real Candidate 2 digest distinct from Candidate 1 and an evaluator-owned
   attestation proving feedback was consumed in the same Builder session.
3. Candidate 2 freeze followed by exactly six real hidden DeepTutor executions
   through the evaluator-owned `gpt-5.6-sol` / `medium` broker.
4. Per-case broker attribution with zero provider/infrastructure failures,
   stable frozen digest, and a valid hidden-after-freeze attestation.
5. Independent Agent-loop Result scoring, independent Code scoring, and formal
   aggregation.

Accordingly, this delivery establishes the code and fail-closed harness needed
for the next run, but it does not claim a formal score.

## One-stop formal orchestration implementation — 2026-09-03

This sibling now contains `harbor/formal_one_stop.py`. No formal run was
started while implementing or validating it.

The entry composes the existing sibling modules rather than replacing their
gates:

1. It stages a Builder-visible package containing only `input/`, both public
   dev cases, and a read-only copy of the pinned product source. Hidden cases,
   evaluator case JSON, old runs, and provider credentials are not staged.
2. It starts one evaluator-owned Builder broker locked to
   `gpt-5.6-sol` / `xhigh`, then launches exactly one Harbor Builder process.
   One evaluator-issued session ID and connection ID are retained for both
   submissions.
3. A Unix-socket submission surface copies each submission into evaluator
   custody, materializes it with `candidate_adapter.py`, and executes both
   `dev_001` and `dev_002` through the real DeepTutor lower launcher and a
   separate `gpt-5.6-sol` / `medium` public broker.
4. `TwoRoundController` remains authoritative for Candidate-round consumption,
   exact feedback-digest acknowledgement, distinct materialized Candidate
   digests, atomic regular-file freeze, and same-session attestation.
   Infrastructure-invalid public attempts remain non-consuming; Candidate
   delivery/build/capability/behavior failures remain Candidate outcomes.
5. Only after a valid Candidate 2 freeze, the entry starts a new independent
   zero-call `gpt-5.6-sol` / `medium` hidden broker and invokes the existing
   `run_hidden.py` executor for exactly `test_001` through `test_006`.
6. Builder, public, and hidden broker logs/stats are run-local. The controller
   and all broker process groups are closed in `finally`, and a cleanup
   attestation is written. The entry deliberately leaves formal Result and
   Code score unclaimed.

Credential and attribution hardening needed by the one-stop entry was kept
minimal:

- `agentloop/broker.py` now supports an explicit evaluator-owned Builder role;
  the default remains the lower role. The selected role fixes the broker model
  and reasoning effort and records them in health/stats evidence.
- `agentloop/lower_agent_entry.py` no longer passes the evaluator's complete
  host environment to the Candidate runtime. It passes a small runtime
  allowlist and explicitly supplies only `broker-only-placeholder` for model
  access. Provider failures are classified separately from broker/protocol
  failures.
- `agentloop/self_test.py` now requires the one-stop entry to exist.

Validation performed after these changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
  harbor/formal_one_stop.py \
  agentloop/broker.py \
  agentloop/lower_agent_entry.py \
  agentloop/self_test.py
```

Result: `PY_COMPILE=PASS`.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 agentloop/self_test.py
```

Result:

```text
SELF_TEST=PASS inventory=2+6 lifecycle=2-round hidden-after-freeze=guarded code_total=100 provider_calls=0 status=PARTIAL
```

The following CLI checks also passed:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 harbor/formal_one_stop.py --help
PYTHONDONTWRITEBYTECODE=1 python3 agentloop/broker.py --help
PYTHONDONTWRITEBYTECODE=1 python3 agentloop/lower_agent_launcher.py --help
PYTHONDONTWRITEBYTECODE=1 python3 agentloop/run_hidden.py --help
```

Observed status: `FORMAL_HELP=PASS`, `BROKER_HELP=PASS`,
`LOWER_LAUNCHER_HELP=PASS`, and `HIDDEN_HELP=PASS`.

### Remaining runtime blockers and evidence gaps

The orchestration is statically ready but the sibling remains `PARTIAL` and
both formal axes remain `N/A` because the required real run has not occurred.
The remaining evidence is:

- Harbor and its Builder image must be available, and the evaluator credential
  must complete real provider calls through the Builder and lower brokers.
- The task-local DeepTutor Python must retain Python 3.11–3.13, Pydantic v2,
  OpenAI, PyYAML, Jinja2, defusedxml, aiosqlite, and importable DeepTutor CLI
  dependencies. Missing imports remain runtime infrastructure failures.
- The Responses compatibility adapter still needs real upstream function-call
  traffic through DeepTutor's production mastery loop; syntax and offline
  adapter checks are not a substitute.
- A real uninterrupted Builder process must submit Candidate 1, complete both
  public dev cases, consume evaluator feedback, and submit a distinct
  Candidate 2. Existing migrated/pilot Candidate directories cannot establish
  this provenance.
- Candidate 2 must pass the runtime/product capability probe, freeze, and then
  execute all six hidden cases with a fresh hidden broker, successful broker
  calls, stable frozen digest, and no provider or infrastructure failures.
- Independent Result evaluation, independent Code judging, and formal
  aggregation remain outside this implementation and were not run or claimed.

## English release source pin (2026-10-03)

The source digest recorded above describes the historical Chinese as-run tree. The public English upstream source uses `1dda8c9737d0997b5efc9fa7a85348f96b05b73e67d5f5b8ac7625cd4eae57bc`, verified with the task protocol digest and `en-upstream.sha256`. Historical benchmark and run digests in this report remain unchanged.
