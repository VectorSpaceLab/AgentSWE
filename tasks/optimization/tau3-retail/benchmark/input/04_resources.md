# Resources

Use the pinned τ³-bench text/half-duplex retail domain with local resettable data. The isolated native runtime receives `GATEWAY_API_KEY` via the read-only dotenv path in `HARNESS_CREDENTIAL_FILE` and uses the Responses API at `https://gateway.example.com/v1/responses` with evaluator-locked model `gpt-5.6-sol`. The evaluator fixes the user simulator model and runtime model; the Builder-created harness may optimize agent prompts, state, tool policy, planning, memory, and recovery, but may not replace the provider/model. Budget: 25 turns, 4 tool calls per turn, 900 seconds, and 150k tokens per row.

Credentials are injected only at evaluation time. The public development package contains no secret values. Never put credentials in predictions, reports, URLs, or logs. The evaluator records actual model calls, tool calls, actions, DB state, conversation trace, wall time, reward components, and failures.
