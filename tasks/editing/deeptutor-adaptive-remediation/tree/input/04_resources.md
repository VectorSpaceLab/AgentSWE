# Resources

## Builder

Builder resources are controlled by the outer evaluation and are distinct from
the lower-agent resources. The Builder must report actual use in
`run_report.json` and must never copy credentials into the Candidate.

## Lower agent

- Product: the materialized patched DeepTutor repository.
- Model: exactly `deepseek-flash`.
- Reasoning effort: exactly `high`.
- Transport: evaluator-owned `/v1/responses` broker.
- Candidate token/key: `broker-only-placeholder` only.
- Real credential: evaluator broker only; never mounted into the Candidate or
  lower container.
- Network: broker endpoint only. Product tools and state operations are offline.
- Per-case default budget: 12 model calls and 240,000 total tokens. Both limits
  are counted per case.
  - Tokens are counted as the provider bills them: every call is charged for its
    full request, and the lower agent re-sends the whole prompt (system prompt,
    all registered tool schemas and the accumulated turn history) on every call.
    A turn is therefore charged in full, not as an increment over the previous
    turn. Budget for roughly 20,000 tokens per turn across the 12 turns.
  - A further call is admitted only while the case's counted total is still
    below 240,000. The response that crosses the limit is measured and
    reported, not hidden.
  - The formal controller may lower these values but may not change model or
    effort.
- Filesystem: read-only patched repository, fresh writable DeepTutor state root,
  and fresh output directory. No benchmark/evaluator/oracle/hidden mounts.

Broker statistics must record calls, successful calls, failures, input tokens,
output tokens, total tokens, model overrides, and last failure classification.
