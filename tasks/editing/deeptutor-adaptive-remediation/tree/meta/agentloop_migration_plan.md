# Agent-loop migration plan — owner-18

## Provenance and status

- Authoritative source: `@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/a0src`
- Verified product-source digest: `56d107ba0798b0c2f0f852db4af6b8860c75b08f2f43e8d2971ad15e8202c862`
- Verified benchmark-root digest: `b3b510684520e23a237035a60990c46843157e564edd360325009d54791ad078`
- Write target: this sibling only.
- Current status: `PARTIAL`. Stage A is implemented, but no real successful
  lower-model broker call has been run.

## Real product entry and model surface

DeepTutor's production mastery capability is not the deterministic benchmark
driver. `MasteryPathCapability.run()` marks a turn as mastery mode, resolves a
path ID, constructs `AgenticChatPipeline`, and enters the product's own
`AgentLoop`. The loop builds OpenAI function schemas from the process registry,
mounts `MASTERY_TOOL_TYPES`, calls the configured model, dispatches model-selected
tools, persists state through `LearningStore`, and streams the final response.

DeepTutor's agentic client speaks OpenAI Chat Completions with streaming and
native function calls. The evaluator model contract is Responses. The minimal
adapter in `responses_client_adapter.py` therefore presents the exact client
surface DeepTutor expects while translating messages, tool calls, and tool
outputs to `/v1/responses`. The adapter returns streamed-shaped chunks to the
unchanged DeepTutor loop after receiving an evaluator-broker response. It has
no product policy and no access to the upstream credential.

## Case, tool, state, artifact

- Case input: a learner-facing remediation/recovery request plus runtime-generated
  path aliases, evidence timestamps, and bounded task constraints.
- Product tools: registered mastery status/quiz/grade plus the Candidate-added
  remediation, review, event, handoff, policy, snapshot, attestation, witness,
  verify, and chain-audit surfaces.
- State: one fresh `PathService` root per case; state is seeded through patched
  product tools, not by manufacturing claimed tool results.
- Trajectory: DeepTutor StreamBus events, including model call status, model
  authored content, tool calls, and tool results.
- Final artifact: when `DEEPTUTOR_AGENT_RESULT` is present, the patched
  DeepTutor product persists the exact model-authored final JSON object to that
  product-workspace path. The evaluator captures that file only after the
  process exits and rejects a missing, malformed, wrong-case, or synthesized
  object; raw tool events remain separate in `trajectory.jsonl`.

The product-side writer is included in `delivery/solution.patch` at
`deeptutor/agents/chat/agent_loop.py`. The current delivery was materialized
against the verified product-source digest and passed `git apply --check`,
`git apply`, and targeted compilation. A provider-backed execution is still
required to prove the writer is reached by the real model-selected loop.

## Dynamic oracle

For every run, evaluator code creates fresh path/topic/concept/event/subscriber/
challenge identifiers and a bounded synthetic question catalog. It records the
actual accepted product responses, state hashes, revision/high-water values,
snapshot and witness evidence in an evaluator-only oracle. Scoring requires the
agent's claims to match those real values. Non-empty random strings do not count.

Cases share capability structure but not concrete values. Hidden cases vary the
lifecycle combination: response loss, stale clients, tamper, cross-path probes,
reset/redo, and stale witness heads. None is a lexical copy of Codex residual's
byte-stream cases.

## Isolation

The Builder package is physically trimmed to `input/` and `dev_cases/`. The
Candidate delivery contains only `solution.patch`, `edit_report.json`, and
`run_report.json`. The broker alone reads the real credential. Lower execution
receives the placeholder `broker-only-placeholder` and an endpoint controlled by
the evaluator.

The lower container mounts only:

- materialized patched DeepTutor repository, read-only;
- fresh path-local state and output directories;
- one generated prompt;
- evaluator launcher plus the credential-free Responses adapter.

It does not mount benchmark/evaluator roots, case JSON, dynamic oracle, hidden
inventory details, another case, prior runs, or the credential file. The
launcher writes an isolation manifest and rejects overlapping mount roots.

## Build/materialize strategy

`candidate_adapter.py` validates the exact delivery contract, checks safe
repository-relative patch paths, copies the authoritative repository, applies
the patch with `git apply --check` and `git apply`, and runs targeted Python
compile checks. It records source, delivery, materialized tree, and patch
digests. Dependency installation and heavyweight product startup are excluded
from Stage A.

## Bounded accepted-submission lifecycle

`two_round_controller.py` is retained as a compatibility module, while the
live controller enforces one evaluator-attested Builder connection with the
locked Builder model/effort, one through ten distinct consuming submissions,
two dev cases on every accepted submission, and exact fresh-feedback digest
consumption before later submissions. Infrastructure-invalid evaluations are
archived without consuming a round. Candidate-side delivery/build/contract
failures consume a round. Builder exit or the round limit freezes the latest
accepted Candidate as an atomic regular-file, read-only snapshot.
`run_hidden.py` verifies the freeze digest, immutable snapshot, fresh locked
broker, and a strictly later hidden start time.

## Result and Code

Agent-loop Result is a seven-dimension 100-point behavioral rubric over real
model calls, DeepTutor tool trajectory, dynamic facts, persisted provenance,
the final learner artifact, honest recovery, and isolation/safety. The old
native score remains legacy evidence only.

Code uses the required independent eight dimensions and exact weights
15/20/15/15/10/10/10/5. `code_score_runner.py` builds a bounded frozen-source
evidence pack and validates an evaluator-owned judge contract. It deliberately
does not read Agent-loop Result files or scores.

## Risks and pilot estimate

- Runtime dependency risk: the base machine does not expose a dedicated
  DeepTutor venv; a pilot should use a run-private Python 3.11 prefix containing
  OpenAI, Pydantic v2, pydantic-settings, PyYAML, Jinja2, and the focused product
  dependencies.
- API compatibility risk: the Responses adapter is syntax/self-tested but has
  not yet been exercised against a real upstream response containing function
  calls.
- Product import risk: broad DeepTutor imports can pull optional services. The
  launcher constrains the turn to mastery tools and should be tested first with
  a baseline/frozen Candidate.
- Behavioral cost: one simple pilot dev plus one hidden smoke is expected to use
  roughly 4-10 lower-model calls, 20k-70k total tokens, 8-20 minutes wall time,
  2-4 CPU cores, 4-8 GiB RAM, and less than 3 GiB temporary disk when a prepared
  Python prefix is reused. This is an estimate, not an observed run.

## English release source pin (2026-10-03)

The source digest recorded above describes the historical Chinese as-run tree. The public English upstream source uses `1dda8c9737d0997b5efc9fa7a85348f96b05b73e67d5f5b8ac7625cd4eae57bc`, verified with the task protocol digest and `en-upstream.sha256`. Historical benchmark and run digests in this report remain unchanged.
