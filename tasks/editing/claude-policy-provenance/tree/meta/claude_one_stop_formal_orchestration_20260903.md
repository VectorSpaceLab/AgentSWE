# Claude Agent-loop one-stop formal orchestration closeout

Date: 2026-09-03

## Outcome

The Claude policy-provenance Agent-loop sibling now contains an opt-in,
one-stop formal orchestration entry point. The implementation is ready to run
the following evidence lifecycle when evaluator-owned credentials and six
externally issued hidden JSON case specifications are supplied:

```text
one Harbor Builder invocation, gpt-5.6-sol/xhigh
-> Candidate 1 delivery preflight and materialization
-> dev_001 + dev_002 through the Candidate Claude plugin boundary
-> evaluator-owned, content-addressed feedback
-> same-session feedback acknowledgement
-> distinct Candidate 2
-> Candidate 2 dev_001 + dev_002
-> immutable Candidate 2 freeze
-> fresh independent gpt-5.6-sol/medium hidden broker
-> test_001 .. test_006 after freeze
-> hidden attestation and three broker-stat artifacts
-> scoped container cleanup attestation
```

No real formal, provider-backed Builder, public, or hidden run was started as
part of this work. Formal Result and formal Code score remain `N/A`.

## Implemented files

- `harbor/formal_one_stop.py`
  - Refuses to run unless `--run-formal` is explicit.
  - Refuses an existing run directory and never deletes old runs.
  - Performs credential, Harbor launcher, hidden inventory, and hidden
    isolation preflight checks.
  - Stages a physically trimmed Builder package containing only `input/` and
    public development cases.
  - Creates one Harbor Builder task locked to `gpt-5.6-sol/xhigh`.
  - Exposes evaluator-owned `validate`, `submit`, and `status` operations over
    one authenticated Unix socket for the uninterrupted Builder invocation.
  - Validates exactly `solution.patch`, `edit_report.json`, and
    `run_report.json`; patch/build failures do not consume a Candidate round.
  - Runs both public cases for each accepted Candidate against the actual
    Candidate hook and inspector through the existing lower launcher.
  - Creates evaluator-owned feedback bound to Candidate 1, Builder session,
    and Builder connection. Candidate 2 must acknowledge the exact feedback
    digest and have a different product digest.
  - Starts the hidden broker only after Candidate 2 freeze. The existing hidden
    executor verifies it is independent, `medium`, and zero-call before all six
    hidden cases execute.
  - Saves Builder, public-lower, and hidden-lower broker stats before cleanup.
  - Separates credential/preflight, Builder launcher/container/mount, broker
    protocol/delivery, provider, public/hidden infrastructure, Candidate
    preflight/build/distinctness, and Candidate product/behavior failures.
  - Cleanup removes only the three deterministic broker containers and Harbor
    containers whose mount source is inside this new run directory; removal is
    rechecked with `docker inspect`.

- `harbor/builder_broker.py`
  - Evaluator-owned Builder-only Responses broker locked to
    `gpt-5.6-sol/xhigh`.
  - Supports Responses and the minimal chat compatibility surface used by the
    Builder runtime.
  - Holds the provider credential internally; the Builder sees only
    `broker-only-placeholder`.
  - Exposes redacted health and stats with provider, protocol, and delivery
    failure counters.

- `harbor/__init__.py`
  - Marks the sibling-local Harbor orchestration package.

- `agentloop/evaluator/builder_protocol.py`
  - Adds a strictly limited Candidate-1 provisional witness gate for live
    orchestration.
  - Retains the default final gate requiring exactly two same-session,
    same-connection submissions, distinct Candidate digests, and feedback
    consumption/binding for Candidate 2.

- `agentloop/evaluator/controller.py`
  - Allows the evaluator to bind the stage-appropriate Builder witness.
  - Treats public broker/provider/launcher invalidity as infrastructure
    evidence and does not consume the Candidate round.
  - Preserves the existing freeze and hidden-after-freeze gates.

- `agentloop/evaluator/broker.py`
  - Makes provider-key selection deterministic when a credential file contains
    multiple supported keys: evaluator-specific key, then GATEWAY, then OpenAI.

- `agentloop/self_test.py`
  - Adds provisional-versus-final Builder witness regression coverage.
  - The reported offline check count is now 31.

## Verification performed

The final closeout validation was run from this sibling:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
  harbor/formal_one_stop.py \
  harbor/builder_broker.py \
  agentloop/evaluator/broker.py \
  agentloop/evaluator/builder_protocol.py \
  agentloop/evaluator/controller.py \
  agentloop/self_test.py

PYTHONDONTWRITEBYTECODE=1 python3 agentloop/self_test.py

PYTHONDONTWRITEBYTECODE=1 \
  python3 harbor/formal_one_stop.py --self-test

PYTHONDONTWRITEBYTECODE=1 \
  python3 harbor/formal_one_stop.py --help

PYTHONDONTWRITEBYTECODE=1 \
  python3 harbor/builder_broker.py --help
```

Observed result:

```text
PY_COMPILE=PASS
SELF_TEST=PASS checks=31 network_calls=0 formal_result_claimed=false
ONE_STOP_SELF_TEST=PASS
ONE_STOP_SELF_TEST_NETWORK_CALLS=0
ONE_STOP_SELF_TEST_DOCKER_STARTED=false
ONE_STOP_SELF_TEST_FORMAL_EXECUTION_STARTED=false
BUILDER_PROTOCOL=gpt-5.6-sol/xhigh
LOWER_PROTOCOL=gpt-5.6-sol/medium
PUBLIC_INVENTORY=dev_001,dev_002
HIDDEN_INVENTORY=test_001..test_006
CANDIDATE_CREDENTIAL=broker-only-placeholder
FORMAL_ONE_STOP_HELP=PASS
BUILDER_BROKER_HELP=PASS
MINIMAL_CLOSEOUT_VALIDATION=PASS
```

Earlier in the same implementation pass, the original evaluator fixture was
also invoked with its required output argument and exited successfully:

```bash
tmpdir=$(mktemp -d)
PYTHONDONTWRITEBYTECODE=1 \
  python3 evaluator/harness/evaluate_suite.py \
  --self-test --output-dir "$tmpdir"
rm -rf "$tmpdir"
```

This was an offline fixture run. It is not formal Agent-loop evidence.

## Formal invocation contract

A future evaluator-authorized formal run uses a new output directory and an
external evaluator-owned directory containing exactly
`test_001.json` through `test_006.json`:

```bash
python3 harbor/formal_one_stop.py \
  --run-formal \
  --run-dir @@AGENTSWE_LEGACY_DATA@@/<new-claude-formal-run> \
  --hidden-cases-dir @@AGENTSWE_LEGACY_DATA@@/<evaluator-issued-hidden-json> \
  --credential-file /path/to/evaluator-owned.env
```

The command deliberately rejects `test_cases/` in this sibling as the hidden
JSON source. This prevents the Builder/Candidate package from becoming an
oracle-bearing formal path.

## Remaining blockers and non-claims

The one-stop orchestration implementation itself has passed only offline
validation. A complete formal measurement still requires:

1. An evaluator-authorized real Harbor run with a usable provider credential.
2. Six evaluator-issued lower-agent JSON case specs outside this benchmark
   sibling, named exactly `test_001.json` through `test_006.json` and containing
   only Candidate-safe runtime inputs/assets.
3. Successful completion of the same-session Candidate 1/public/feedback/
   Candidate 2/freeze lifecycle under the real provider.
4. A fresh hidden broker and six real post-freeze hidden executions with the
   generated hidden-after-freeze attestation.
5. Independent Result judging and independent Code judging after the complete
   evidence chain exists.

Current truthful status:

```text
one-stop implementation: complete and offline-verified
real Builder/provider run: not executed
real public dev lifecycle: not executed
real Candidate 2 freeze: not produced
real hidden execution: 0/6
formal Result: N/A
formal Code score: N/A
formal aggregation: not started
```
