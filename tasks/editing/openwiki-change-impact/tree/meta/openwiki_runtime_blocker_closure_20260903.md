# OpenWiki Agent-loop runtime blocker closure — 2026-09-03

## Scope and status

This work was limited to:

- `@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree`
- the new evidence root
  `@@AGENTSWE_LEGACY_DATA@@/0902-edit-agentloop-pilot/openwiki/runtime-blocker-20260903-node22-smoke-v1`

No authoritative source, `@@AGENTSWE_LEGACY_HARBOR@@/0830-edit-v2`, or old formal run
was modified. This was a runtime/product-entry smoke only. It did not create a
Candidate 1 or Candidate 2, freeze a Candidate, execute hidden cases, or claim
a Result/Code score.

Current formal status remains:

```text
Agent-loop Result: N/A
Code score: N/A
Formal run: not started
```

## Repository instructions read

The applicable repository instruction file was read first:

```text
input/repository/AGENTS.md
```

Following that instruction, `input/repository/openwiki/quickstart.md`, the CLI
usage documentation, and the credential/update operation documentation were
also inspected. They identify `dist/cli.js` as the production product entry and
`better-sqlite3` as an installation-time native dependency. Source and tests
remain authoritative; generated `openwiki/` pages were not edited.

## Runtime repair

A task-local Node runtime was prepared at:

```text
.runtime/node-v22.12.0-linux-x64
```

The official Node archive was downloaded and checked against the published
SHA-256 value:

```text
archive: node-v22.12.0-linux-x64.tar.xz
expected: 22982235e1b71fa8850f82edd09cdae7e3f32df1764a9ec298c72d25ef2c164f
observed: 22982235e1b71fa8850f82edd09cdae7e3f32df1764a9ec298c72d25ef2c164f
Node: v22.12.0
NODE_MODULE_VERSION: 127
```

`better-sqlite3@12.11.1` was rebuilt under that exact runtime with
`node-gyp`. The produced native binary is:

```text
.runtime/candidate-smoke/repository/node_modules/.pnpm/
  better-sqlite3@12.11.1/node_modules/better-sqlite3/
  build/Release/better_sqlite3.node

SHA-256:
0c588e6e25c6755e314810c03a35172a5dea44b6da62264871bad51bc9f4f4e4
```

The native validation was stronger than a bare `require()`: it opened an
in-memory SQLite database, created a table, inserted and selected
`node22-native-ok`, and closed the database. The observed record was:

```json
{
  "node": "v22.12.0",
  "modules": "127",
  "sqlite": "node22-native-ok",
  "closed": true
}
```

The reproducible runtime preparation entry is now:

```bash
agentloop/evaluator/prepare_node22_runtime.sh
```

It pins the archive checksum, Node version, ABI, npm cache, source rebuild, and
real SQLite write/read/close probe.

## Failure attribution repair

`agentloop/evaluator/lower_agent_launcher.py` now performs an evaluator-owned
runtime preflight before it reads broker stats or starts the OpenWiki product.
The preflight explicitly uses the sibling-local Node 22 binary and locates the
actual pnpm `better-sqlite3` package rather than assuming a root dependency
link.

If the runtime, package, native binding, or database round trip is unavailable,
the result is now:

```text
classification = native_binding_failure
infrastructure_invalid = true
candidate_classification = null
failure_attribution.owner = evaluator_runtime
failure_attribution.phase = pre_broker_product_entry
product_started = false
broker_calls = 0
```

An automated guard asserts that broker access is not attempted in this path.
This prevents a pre-broker evaluator/runtime defect from being charged to
Candidate behavior.

`agentloop/candidate_adapter.py` also pins its pnpm/build process to Node
22.12.0 and performs the explicit native rebuild and SQLite round trip after
the intentionally script-free offline dependency installation. A failure in
that evaluator-owned native preparation is infrastructure-invalid rather than
a Candidate build zero.

## Real broker and product-entry smoke

Evidence root:

```text
@@AGENTSWE_LEGACY_DATA@@/0902-edit-agentloop-pilot/openwiki/
  runtime-blocker-20260903-node22-smoke-v1
```

### Direct evaluator-broker probe

A minimal request was sent to the already-running evaluator-owned broker at
`127.0.0.1:33507`. The request used only:

```text
Authorization: Bearer broker-only-placeholder
model: gpt-5.6-sol
reasoning_effort: medium
```

Result:

```text
HTTP 200
response status = completed
response model = gpt-5.6-sol
usage object present = true
```

Evidence:

```text
broker-probe/request.json
broker-probe/response.headers
broker-probe/response.json
broker-probe/http-status.txt
```

The Candidate-facing request contained no real provider credential. The real
provider credential remained inside the external evaluator broker and was not
read, copied, mounted, or logged by this sibling.

### OpenWiki `dev_001` product-entry smoke

The first attempt stopped before the launcher because the public runner used
the wrong template path. It is retained as evaluator failure evidence:

```text
public-dev-product-smoke.command.stderr
```

The runner path was corrected from the nonexistent root
`evaluator/case_templates.json` to
`agentloop/evaluator/case_templates.json`.

The second attempt used a fresh dynamic `dev_001` nonce and fixture, the real
OpenWiki entry `dist/cli.js`, Node 22.12.0, and the evaluator broker chain. A
credential-free smoke observer exposed schema-v2 counters to the launcher while
forwarding requests to the evaluator-owned broker. The observer held no
provider credential; both Candidate-to-observer and observer-to-broker hops
used `broker-only-placeholder`.

Observed case snapshot at the 240-second limit:

```text
runtime preflight = runtime_ready
product started = true
broker calls = 13
successful calls = 10
provider failures = 2
broker protocol failures = 0
forced lower-model overrides = 13
Candidate provider credential mounted = false
product result artifact = absent
process outcome = timeout
classification = provider_failure
infrastructure_invalid = true
```

The product modified the disposable workspace, including
`openwiki/api/names.md`, proving that execution advanced beyond module loading.
It did not produce `agent_result.json`, so the smoke is not a Candidate success
and cannot be used as public-dev feedback or formal Result evidence.

One already-forwarded request completed after the launcher's timeout snapshot.
Therefore the raw observer file ended at `13 calls / 10 successful / 3 provider
failures`, while the immutable per-case `broker_after.json` records `13 / 10 /
2`. Both raw values are retained. The original observer also accumulated
integer HTTP statuses (`last_upstream_status=3506`) because its generic update
method treated every integer as a counter. That post-smoke observer bug was
fixed and covered by a regression test; the product smoke was not rerun, so the
raw evidence was not rewritten.

Primary evidence:

```text
public-dev-product-smoke-v2/lower/runtime-preflight.json
public-dev-product-smoke-v2/lower/runtime-preflight.stdout.log
public-dev-product-smoke-v2/lower/launcher_request.json
public-dev-product-smoke-v2/lower/broker_before.json
public-dev-product-smoke-v2/lower/broker_after.json
public-dev-product-smoke-v2/lower/launcher_result.json
public-dev-product-smoke-v2/lower/trajectory.json
public-dev-product-smoke-v2/lower/workspace/
observer/broker_stats.json
```

Relevant immutable evidence digests:

```text
broker_before.json:
b3e95bfc2d80986acda732a793cd7593afd11bfe1e53d07b5ede2f5dc86e0c02

broker_after.json:
96441d0914be67ee7262a6991e9c840724ae60195eba9d3a110a363de1f33d27

runtime-preflight.json:
92236d084ea676c612a74fc8d5be00d881134b60fd81622f2984b518f491f8da

launcher_request.json:
f0988356b1ffac70b8f5759db1658cc5f8b1a394fd2e618c6d66dd8d1522aec9
```

## Validation performed

```bash
bash -n agentloop/evaluator/prepare_node22_runtime.sh

python3 -m py_compile \
  agentloop/candidate_adapter.py \
  agentloop/evaluator/lower_agent_launcher.py \
  agentloop/evaluator/smoke_observer.py \
  agentloop/dev_cases/run_public.py \
  agentloop/evaluator/tests/test_agentloop_guards.py

PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest -v agentloop.evaluator.tests.test_agentloop_guards

PYTHONDONTWRITEBYTECODE=1 python3 agentloop/self_test.py
```

Results:

```text
shell syntax = PASS
Python compile = PASS
guard tests = 18/18 PASS
static self-test = PASS
smoke observer and OpenWiki product processes = stopped
```

The tests include:

- real Node 22 and SQLite database round-trip validation;
- pre-broker native failure never touching broker stats;
- evaluator/runtime ownership and infrastructure-invalid attribution;
- placeholder-only Candidate environment;
- provider/broker failure-counter separation;
- observer endpoint routing and HTTP-status overwrite semantics.

## Files changed

```text
agentloop/candidate_adapter.py
agentloop/dev_cases/run_public.py
agentloop/evaluator/lower_agent_launcher.py
agentloop/evaluator/prepare_node22_runtime.sh
agentloop/evaluator/smoke_observer.py
agentloop/evaluator/tests/test_agentloop_guards.py
agentloop/self_test.py
meta/openwiki_runtime_blocker_closure_20260903.md
```

Task-local runtime artifacts were created under `.runtime/`, including the
Node 22.12.0 distribution, verified download material, npm cache, and rebuilt
native binding.

## Remaining blockers

The native runtime blocker is closed. The remaining blockers are outside this
runtime-only smoke:

1. No real same-session `gpt-5.6-sol/xhigh` Builder lifecycle has produced
   Candidate 1, consumed two infrastructure-valid public-dev feedback records,
   and produced a distinct Candidate 2.
2. The product smoke encountered upstream/provider failures and hit the bounded
   240-second timeout. It produced no terminal `agent_result.json`; therefore it
   cannot authorize feedback, freeze, or scoring.
3. Candidate 2 has not been frozen and the six hidden cases have not been run
   after freeze.
4. A future formal run must use the independent evaluator-owned broker
   lifecycle, not the smoke observer or a shared broker.
5. Agent-loop Result and Code score remain `N/A` until the complete formal
   evidence chain exists.
