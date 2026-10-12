# Migration note

The legacy benchmark exercised benchmark-owned Python scenarios and a native
handoff harness. v1 retains those source fixtures only as provenance and
mechanism/build evidence; Agent-loop Result is instead defined around the real
OpenClaw Gateway and embedded agent.

The product-specific behavioral unit is a synthetic channel task. A case
service owns dynamic route IDs, lease generations, provider acceptance states,
attachment bytes, and callback tokens. The lower agent may observe only the
case client projections and returned receipts. The oracle remains outside the
Candidate. `agent_result.json` is the only authored artifact; raw Gateway/RPC
events are trajectory evidence and are not themselves authored claims.

The source package is copied into this sibling to preserve the Builder input
contract. No source file in the authoritative directory is modified by this
migration.

