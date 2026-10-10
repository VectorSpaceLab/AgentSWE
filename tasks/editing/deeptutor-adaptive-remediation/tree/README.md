# DeepTutor Adaptive Remediation — Agent-loop Edit v1

This sibling migrates the authoritative DeepTutor Edit benchmark from a native
component Result to a real lower-agent Result. The original source, the 0830
staging tree, old formal runs, shared framework files, and the main manifest are
read-only.

## What is measured

The lower agent is DeepTutor itself:

```text
MasteryPathCapability.run
  -> AgenticChatPipeline.run
  -> DeepTutor AgentLoop
  -> evaluator-owned deepseek-flash / high broker
  -> registered MASTERY_TOOL_TYPES
  -> path-local learning state
  -> agent-authored agent_result.json
```

The lower broker runs `deepseek-flash` at reasoning effort `high`, with the
per-case defaults disclosed in `input/04_resources.md`: **12 model calls and
240,000 total tokens per case**. The whole prompt is re-sent and counted on
every turn, so the token ceiling is sized for 12 whole turns (~20,000 tokens
each), not for 12 increments; the call budget is the binding limit.

The evaluator-side Responses compatibility adapter only converts DeepTutor's
OpenAI Chat-Completions client contract to the locked Responses transport. It
does not plan, select tools, fabricate tool results, or write the final answer.

The legacy `scenario_driver.py` semantics remain useful as a patch/build gate,
state seeder, and auxiliary oracle. They are not an Agent-loop Result by
themselves.

## Fixed lifecycle

1. The evaluator opens one attested Builder connection locked to
   `deepseek-flash` / `max` and records its witness.
2. That uninterrupted Builder session submits between one and ten Candidates.
3. Every accepted Candidate is materialized and run on both `dev_001` and
   `dev_002`.
4. Evaluator-owned feedback returns both public outcomes, assertions, build
   evidence, broker calls/failures/tokens, and infrastructure classification.
5. For every later submission, the same attested Builder connection consumes
   the exact latest feedback and submits a distinct Candidate digest.
6. A dev mean above 60 is recorded as `dev_passed`; it never auto-freezes.
   On Builder exit or the maximum accepted round, the latest accepted Candidate
   is atomically frozen as a regular-file, read-only private snapshot.
7. Hidden execution is rejected until the freeze manifest verifies the
   feedback digest, same-session witness, immutable snapshot, and strict
   post-freeze start time.
8. All hidden cases use the exact frozen latest-Candidate digest and a fresh
   zero-call evaluator broker.

Infrastructure-invalid attempts do not consume a Candidate round. Candidate
delivery, patch, build, contract, or policy failures do consume the round and
may score zero. Result and Code are reported independently and are never added
or averaged.

## Inventory

- Public development: `dev_001`, `dev_002`.
- Hidden inventory: `test_001` through `test_006`.
- Runtime case definitions and dynamic oracle controls are evaluator-owned
  under `evaluator/cases/`; they are excluded from the Builder package.

The Builder-visible package must contain only `input/` and `dev_cases/`. A
lower case receives a read-only materialized patched repository, a fresh
DeepTutor state root, a fresh output directory, the case prompt, evaluator
launcher code, and a placeholder broker token. It does not receive the
benchmark root, hidden case definitions, oracle files, another case's state,
prior runs, or the real model credential.

## Stage A commands

These commands are intentionally networkless and do not start Docker, a model
broker, a lower-agent smoke, or a formal run:

```bash
python3 agentloop/self_test.py
python3 -m compileall -q agentloop
python3 -m json.tool provenance_manifest.json >/dev/null
```

No behavior score is publishable until a later simple pilot observes at least
one successful evaluator-owned `deepseek-flash/high` broker call and verifies one
post-freeze hidden smoke. Until then this sibling is `PARTIAL`, not `READY`.

## Entry points

- Candidate materialization/build: `agentloop/candidate_adapter.py`
- Evaluator-owned lower broker: `agentloop/broker.py`
- DeepTutor Responses compatibility seam: `agentloop/responses_client_adapter.py`
- Real DeepTutor lower entry: `agentloop/lower_agent_entry.py`
- Isolated case launcher: `agentloop/lower_agent_launcher.py`
- Accepted-submission controller: `agentloop/two_round_controller.py`
- Hidden-after-freeze runner: `agentloop/run_hidden.py`
- Agent-loop Result evaluator: `agentloop/result_evaluator.py`
- Independent Code runner: `agentloop/code_score_runner.py`

The evaluator-owned hidden executor is invoked explicitly after freeze:

```bash
python3 -m agentloop.run_hidden \
  --freeze-manifest <run_dir>/freeze_manifest.json \
  --output <run_dir>/hidden \
  --broker-endpoint http://127.0.0.1:<port>/v1/responses \
  --python <task-local-python>
```

It refuses a missing or changed accepted-Candidate freeze, runs exactly six hidden
cases, records per-case broker before/after/delta statistics, and writes
`hidden-after-freeze-attestation.json`. A candidate capability gap is a real
candidate outcome; dependency, launcher, evaluator, broker, and provider
failures remain infrastructure classifications. The executor never writes a
formal Result or Code score.

See `meta/agentloop_migration_plan.md` for the product audit, isolation model,
case rationale, known risks, and pilot resource estimate.
