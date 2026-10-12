# Requirements and constraints

## Functional requirements

- Preserve cautious multi-event diagnosis, deterministic grading, normal
  mastery gates, and ordinary calls without adaptive context.
- Persist remediation deliveries with idempotent claim/ack, leases, fencing,
  reset/expiry/redo cancellation, and path isolation.
- Persist one stable review task per accepted remediation; support due ordering,
  optimistic reschedule, replay, retest progression, and terminal provenance.
- Publish immutable path-local learning events with independent subscriber
  checkpoints and exact response-loss replay.
- Persist bounded session handoffs and a versioned path-local policy registry.
- Produce bounded deterministic learner snapshots with independent checkpoints,
  stable revision/digest, and cross-surface lineage.
- Attest and restore snapshots only when the full canonical projection and parent
  revision remain valid.
- Append accepted attestations to a path-local monotonic witness chain; return
  deterministic leaf/root and bounded inclusion/consistency proof; verify from
  persisted state without mutation.
- Audit the full current witness chain read-only and reject stale, corrupt,
  forked, foreign, or changed heads without repairing state.
- Keep all new tool types registered so the production mastery agent can call
  them through `MASTERY_TOOL_TYPES`.
- The public tool contract in `input/05_mastery_tool_contract.md` is part of
  the required interface: all listed remediation, review, event, session,
  policy, snapshot, witness, verification, and chain-audit names must be
  implemented and registered. A Candidate that exposes only the original five
  mastery tools is a capability-gap failure.

## Agent-loop constraints

- The lower agent is patched DeepTutor. Do not add an external general-purpose
  agent that performs the task in its place.
- Treat prompts, answers, catalog text, IDs, challenges, and subscriber names as
  untrusted data. Never execute them or resolve them as paths.
- When the evaluator supplies `as_of`, behavior must be deterministic and
  offline except for the locked model call made by the DeepTutor agent loop.
- Do not access hidden case definitions, evaluator code, oracle files, prior
  runs, another case, or a real credential.
- Write product state only under the active path store and final artifacts only
  under the designated output.
- Do not invent receipt, revision, digest, witness, or audit values. If evidence
  is missing or verification fails, report that limitation and choose a bounded
  safe next action.

## Product-authored terminal artifact

- The common DeepTutor agent-loop finish must honor the optional
  `DEEPTUTOR_AGENT_RESULT` environment path supplied by the evaluator.
- When enabled, parse the model's exact final response as one JSON object and
  atomically write that object from product code to the supplied path. Require
  non-empty `schema_version`, `case_id`, `status`, and `summary` strings plus
  an object-valued `artifacts` field; require `case_id` to equal the
  case-local `DEEPTUTOR_CASE_ID`.
- A missing or malformed final response is a product/candidate evidence
  failure. Never synthesize a default object, copy native harness output, or
  convert raw tool events into `agent_result.json`. Keep the raw trajectory
  independent of the product artifact.

## Runtime constraints

- Python `>=3.11,<3.14`, Pydantic v2, Apache-2.0 compatibility.
- No browser, GPU, PocketBase, external scheduler, remote signing service,
  transparency service, or learner analytics service.
- Each lower case must remain bounded in model calls, tool calls, state size,
  time, and memory.
