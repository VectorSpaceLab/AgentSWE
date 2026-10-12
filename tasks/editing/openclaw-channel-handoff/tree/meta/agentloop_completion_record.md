# OpenClaw sibling Agent-loop integration record

Date: 2026-09-02

Scope: `@@AGENTSWE_EDITING_TASKS@@/openclaw-channel-handoff/tree`

This record covers only the OpenClaw sibling. Authoritative source, 0830
staging, historical runs, and global progress/manifest files were not edited.

## Implemented lifecycle

The formal-capable entry is `harbor/formal_one_stop.py`:

```text
one Builder process/session
→ Candidate 1 submission
→ real OpenClaw dev_001 + dev_002
→ evaluator-redacted feedback
→ same-session feedback acknowledgement
→ Candidate 2 with a different digest
→ Candidate 2 public dev_001 + dev_002
→ immutable Candidate 2 freeze
→ formal-gated hidden test_001 ... test_006
```

`controller/builder_session_controller.py` owns the Builder session token,
session ID, delivery validation, submission digest, feedback digest and order,
acknowledgement, and suite-level Builder attestation. It rejects a repeated
digest, a submission from another session, a Candidate 2 submission before
feedback acknowledgement, incomplete delivery files, unsafe patch paths, and
`PROBE_ONLY_CANDIDATE2_MARKER`.

`evaluator/candidate_runtime.py` builds each Candidate in a disposable runtime,
binds its source digest to the frozen Candidate, and records the compiled
OpenClaw entry/digest. `lower_agent/launcher.py` prefers Candidate-built
`dist/` and package outputs and falls back to the pinned baseline only for old
probe candidates.

`evaluator/hidden_executor.py` has an explicit probe/formal distinction. Formal
mode requires the Builder attestation and runtime manifest, rejects the probe
marker, requires evaluator-owned `gpt-5.6-sol`/`medium` broker protocol,
requires exactly six real case records, and checks trajectory/result/attestation
files and frozen digest stability for every case. It never authors the
Candidate's terminal artifact or computes a score.

`evaluator/public_runner.py` remains only as a legacy probe compatibility path;
it requires `--legacy-probe` and marks its output `run_mode=probe` and
`formal_lifecycle_eligible=false`. It cannot be used as formal Builder proof.

## Static verification performed

All checks below are low-cost and do not start Docker, Harbor, a broker, a
Gateway, or a model call:

```text
python3 -m py_compile harbor/formal_one_stop.py controller/*.py evaluator/*.py lower_agent/*.py broker/*.py infra/*.py adapters/*.py
node --check lower_agent/launcher.mjs
python3 -m unittest -v evaluator.test_agentloop_invariants
python3 infra/self_test.py
python3 harbor/formal_one_stop.py --help
python3 evaluator/public_runner.py --help
```

Observed result:

```text
12 unit tests: PASS
SELF_TEST=PASS
formal_one_stop --help: PASS
legacy public_runner --help: PASS
```

The tests cover immutable freeze/replay, exact 2+6 inventory, redacted private
facts, failure attribution, real WebChat RPC routing, placeholder-only
environment, delivery schema/path validation, probe-marker rejection, direct
legacy-runner rejection, same-session feedback acknowledgement, distinct
Candidate digests, freeze binding, and Builder attestation validation.

The repository snapshot includes historical TypeScript configuration files
with JSON-compatible extensions/comments (for example `tsconfig*.json`) and
the retained probe runtime includes dependency metadata of the same kind.
Those are not parsed as strict JSON by `jq`; this is unrelated to the Python
controller/evaluator JSON artifacts. The newly generated Python JSON outputs
use the atomic writer and are validated by the lifecycle tests.

## Formal status and real blocker

```text
formal Builder run started      = false
formal hidden run started       = false
formal Result claimed           = false
formal Code score claimed       = false
formal aggregation started      = false
```

The real formal blocker is provider readiness, not the lifecycle controller:
the required run needs an evaluator-owned upstream Responses URL/API
credential for the separate `gpt-5.6-sol` brokers. The retained OpenClaw probe
reached the real Gateway, but its three broker attempts were
`503 provider_unconfigured` with zero successful calls. That is provider
infrastructure evidence, not a Candidate score. A later formal run also must
use a fresh same-session Builder run and must not promote the historical probe
Candidate containing `PROBE_ONLY_CANDIDATE2_MARKER`.

No formal score is claimed by this record. Result and Code axes remain
independent and must be aggregated only after complete, independently attested
evidence exists.
