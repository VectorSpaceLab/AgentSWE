# 0905 Edit repair migration note — DeepCode

Date: 2026-09-04

This sibling remains `REPAIR` and formal execution is fail-closed. No paid API
call, Docker workload, or heavy product runtime was started.

Implemented:

- Added the exact shared-loader 2-dev/6-hidden case contract and an
  assertion-to-case primary-axis matrix.
- Rewrote the six hidden goals so each retains one scientific goal and one
  primary lifecycle failure boundary instead of requiring review, execution,
  promotion, audit, and quarantine in every case.
- Added a Create-aligned accepted-submission ledger: up to ten distinct
  accepted digests, both dev cases per accepted round, idempotent duplicates,
  non-consuming structural/infrastructure attempts, recorded-only `mean > 60`,
  and freeze only on `max_dev_rounds` or `builder_exit`.
- Added the common one-stop CLI and uniform `one_stop_summary.json` writer.
- Replaced formal heuristic publication with direct shared
  `result_judge.py` invocation per scoreable hidden and one Create
  `code_eval.py` invocation per frozen Candidate. Result and Code remain
  independent and `combined_score` is null.
- Result judging now receives only a separate evaluator-owned xhigh broker
  endpoint and placeholder bearer. Broker stats are preserved; no real
  credential is passed to the host Result judge or Candidate.

Provider-free verification:

- `python3 -m unittest evaluator/tests/test_0905_repair.py`: 8 tests passed.
- `python3 -m compileall -q evaluator harbor`: passed.
- Static one-stop with `--max-dev-rounds 10 --n-concurrent 1`: passed, zero
  provider calls, valid `one_stop_summary.json`.
- Shared `audit_readiness.py` with temporary outputs: contract/path/prompt
  checks passed; only the required 0905 real hidden smoke is absent.

Remaining blockers:

1. The paid hidden smoke and independent Result-judge cleanup attestation are
   still pending.
2. Run the authorized 0905 hidden smoke and semantic judge once paid calls and
   containers are permitted. Until then readiness must remain `REPAIR`.

The formal one-stop now owns a fresh xhigh Result-judge broker after scoreable
hidden execution, records its before/after stats, and removes plus inspects it
alongside the public, Builder, and hidden brokers. External formal Result-judge
endpoints are rejected, so cleanup ownership cannot be silently delegated.
Docker cleanup is bound to the container IDs returned by the current run;
name collisions fail without preemptively deleting another workload, and an
unknown Docker inspect error cannot be treated as proof of absence. Result and
Code collect independent error sets, so one axis cannot suppress the other.

The live formal Builder bridge is now connected to the generalized ledger:
one continuous session may submit 1–10 distinct accepted Candidates, each
accepted submission receives fresh public feedback, later submissions require
exact same-session feedback consumption, and Builder exit freezes the latest
accepted Candidate. Hidden loading accepts the v2 accepted-submission freeze
manifest. Provider-free self-test passes, including the six-case hidden
executor contract.

The freeze schema and live error wording were subsequently aligned with the
generalized contract: source submission and accepted-count fields now support
1..10 rather than a Candidate-2 constant. A current publishable real hidden
smoke is still required.

The latest current-sibling repair also adds a per-hidden-case
`evidence-manifest.json` with evaluator-owned launcher, broker, isolation,
artifact, result, and attestation paths. Artifact provenance records the
model producer, actual path, byte size, and SHA-256 digest; the case
attestation cannot declare the manifest complete before that manifest exists.
`tools/self_test.py` and the hidden-runner repair tests pass provider-free.

## September 6, 2026 axis-scope repair

The six hidden evaluator manifests previously carried the same 18-assertion,
100-point bundle even when their prompts named different primary failure axes.
That made review, execution, promotion, audit, and quarantine behavior appear
mandatory in every case and weakened case-level attribution. The manifests now
declare separate 100-point assertion scopes: duplicate/retry, lease fencing,
tenant nondisclosure, partial-write reconciliation, stale-owner publication,
and fail-closed blocked publication respectively. The shared assertion
vocabulary remains available for evaluator diagnostics, but excluded
assertions are not included in the case score. `evaluate.py` now initializes
state from the complete vocabulary and emits explicit scored versus excluded
assertion IDs, avoiding both KeyErrors and accidental score expansion. Optional
audit, quarantine, semantic-diff, reconciliation, and ordinary-compatibility
branches execute only when selected. Excluded audit/quarantine results no longer
gate unrelated assertions, and excluded scope/recovery outcomes cannot invoke
the case-local 35-point safety ceiling.

The sibling harness contract test was updated to require a valid subset of the
shared vocabulary rather than forcing every hidden case to use the old
compound rubric, and now guards against excluded-family cross-gating. JSON
validation, self-test, compile checks, and 28 provider-free
repair/harness/finalizer tests pass. This is a static case-design repair only;
no paid smoke or formal score is claimed, and readiness remains `REPAIR`
pending current real hidden evidence.

## September 7, 2026 dependency-runtime boundary repair

The September 5 DeepCode pilot reached the real lower launcher twice, but both
Candidate processes exited before their first model request because the
default `/usr/bin/python3` runtime lacked the product's `loguru` dependency.
The broker correctly recorded zero calls; this was a runtime-provisioning gap,
not evidence of a healthy zero-call Agent.

`harbor/formal_one_stop.py` now resolves an explicit `--python` or
`DEEPCODE_PYTHON` runtime only after verifying the required product imports.
When no usable interpreter is supplied, it invokes the existing evaluator-owned
`evaluator/harness/prepare_task_environment.py` helper to create an isolated
Python 3.12 virtualenv under the current run directory, records the command and
bounded output in `deepcode_runtime_preparation.json`, and refuses to start the
Builder/lower lifecycle unless the prepared environment imports the required
dependencies. The chosen runtime is passed consistently through public and
hidden lower execution and is recorded in the one-stop summary.

Provider-free verification passed:

```text
python3 -m py_compile harbor/formal_one_stop.py evaluator/harness/prepare_task_environment.py evaluator/tests/test_0905_repair.py
python3 -m unittest -v evaluator.tests.test_0905_repair
Ran 10 tests
OK
python3 harbor/formal_one_stop.py --self-test --run-dir /tmp/deepcode-runtime-repair-selftest
```

No provider-backed smoke or formal evaluation was started by this repair. A
fresh current-digest pilot is required to demonstrate positive medium-broker
usage after runtime provisioning.

## September 8, 2026 duplicate-Candidate lifecycle repair

The live `CandidateController` previously materialized a repeated digest into
the next accepted Candidate path and then rejected it. Besides violating the
documented idempotency contract, that path could leave the next slot occupied,
rejected exact response-loss retries before the Builder could recover its
feedback, and checked the accepted-round cap before recognizing a duplicate.

Duplicate detection now uses an evaluator-owned temporary materialization and
runs before freeze, round-cap, feedback-consumption, build, and public-dev
gates. A repeated digest returns the original accepted record object, removes
the temporary attempt, does not create a new Candidate directory or feedback
record, and does not rerun build or dev. A duplicate remains retrievable at the
accepted-round cap and after freeze without changing the frozen Candidate;
only a new digest is rejected once the lifecycle is frozen.

`BuilderSession` now defers later-round acknowledgement checks until the
controller has established that a digest is new. It archives one stable
feedback identity per accepted source submission, returns that original
feedback and digest for retries (including retries of an older accepted
digest), and leaves the currently active feedback chain unchanged. The formal
socket response exposes `idempotent` and `submission_consumed` explicitly.

Provider-free verification passed on September 8, 2026:

```text
PYTHONPYCACHEPREFIX=/tmp/0908-deepcode-lifecycle-pycache python3 -m py_compile evaluator/harness/controller.py evaluator/harness/builder_lifecycle.py harbor/formal_one_stop.py evaluator/tests/test_harness.py
PYTHONPYCACHEPREFIX=/tmp/0908-deepcode-lifecycle-pycache python3 -m unittest -v evaluator.tests.test_harness evaluator.tests.test_0905_repair
Ran 30 tests
OK
```

No provider call, Docker workload, credential access, hidden formal run, or
shared-harbor change was made. Readiness remains `REPAIR` pending fresh
provider-backed evidence outside this provider-free lifecycle repair.

## September 8, 2026 Builder worktree mount repair

The bounded current-002 pilot exposed that the generated Builder instruction
required edits under `/workspace/worktree`, but the Compose task mounted only
the delivery directory at `/workspace/submission`. The Builder could inspect
the read-only public package but had no writable product tree from which to
author a patch, and timed out with an empty `solution.patch` and zero accepted
submissions.

`harbor/formal_one_stop.py` now creates a run-owned writable copy of the public
`input/repository`, initializes a local Git repository with a pinned baseline
commit, and binds it at `/workspace/worktree`. The Candidate delivery directory
remains a separate writable bind at `/workspace/submission`; evaluator and
hidden material remain absent from the Builder mount.

Provider-free verification passed:

```text
PYTHONPYCACHEPREFIX=/tmp/0908-deepcode-mount python3 -m unittest -v evaluator.tests.test_0905_repair evaluator.tests.test_harness
Ran 31 tests
OK

PYTHONPYCACHEPREFIX=/tmp/0908-deepcode-mount python3 -m py_compile harbor/formal_one_stop.py evaluator/tests/test_0905_repair.py
```

The current-002 pilot predates this source repair and is nonpublishable. A new
bounded current-digest pilot is required to exercise the mounted worktree.

## September 8, 2026 mounted-worktree pilot follow-up

The first bounded live exercise of the repaired Builder mount ran at
`@@AGENTSWE_EDITING_RUNS@@/smoke/deepcode/0908-smoke-current-003`. The
generated Compose environment mounted a writable Git-backed product repository
at `/workspace/worktree`, the Builder saw 681 tracked files, and it used
command-scoped `safe.directory=/workspace/worktree` without changing global Git
configuration. This closes the specific current-002 missing-worktree
integration defect.

The real `gpt-5.6-sol/xhigh` Builder nevertheless reached the 1,800-second
bound before delivery. It authored only the unsubmitted run-local
`workflows/traceability_common.py`; `/workspace/submission` remained empty, so
there was no validation attempt, accepted Candidate, public medium lower call,
freeze, hidden execution, Result judgment, or Code judgment. Durable broker
statistics record 14 calls, 13 completed calls, 11 successful calls, two
provider HTTP 502 failures, zero protocol or delivery failures, and one call in
flight at timeout. The partial worktree file is Builder working state, not a
Candidate artifact.

Primary cleanup removed the owned Builder and public broker containers. The
exact Compose working-directory selector then removed the two residual
main/sidecar containers and their empty network, with
`unrelated_containers_touched=false`; the private tmux socket was also removed.
This pilot remains nonpublishable and readiness remains `REPAIR` pending a
current accepted Candidate and real post-freeze hidden/judge evidence.
