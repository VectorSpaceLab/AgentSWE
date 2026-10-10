# Source repository analysis

- URL: `https://github.com/openclaw/openclaw`
- Local authoritative source: `@@AGENTSWE_EDITING_SOURCES@@/14-edit-openclaw-channel-handoff`
- Commit: `9bd66b7f62cbe3866857be4bbbfa4302e378fdad`
- Package: OpenClaw `2026.7.2`; license MIT; Node `>=22.22.3 <23 || >=24.15.0 <25 || >=25.9.0`; pnpm `11.15.1`.
- Runtime: compiled Node Gateway, WebSocket/RPC server, persistent session/state boundary.
- Inputs: channel/session task, case-local synthetic connector actions and dynamic redacted observations.
- Outputs: OpenClaw state/transcript/receipts plus case-bound `agent_result.json` and `run_report.json`.
- Core capabilities: embedded agent invocation, session/channel/thread routing, durable state, delivery and recovery semantics.
- Excluded: real channel accounts, real provider/channel traffic, UI, remote databases, cloud synchronization, hidden evaluator data.
- Security: route identity and visibility are authoritative; grants, tokens, provider identities, content bodies, credentials, evaluator oracle, and hidden cases stay outside Candidate.
- Benchmark abstraction: edit OpenClaw's real Gateway/agent boundary so handoff claims survive rotation, restart, ambiguity, and side-effect recovery; implementation layout and old native scenarios are not required.
