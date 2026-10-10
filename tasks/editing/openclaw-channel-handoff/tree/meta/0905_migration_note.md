# 0905 Edit repair migration note — OpenClaw channel handoff

Date: 2026-09-04

This sibling remains `REPAIR`; no paid provider call, Docker workload, or formal
hidden run was started during the repair.

The feedback acknowledgment and accepted-submission gates now preserve one
continuous Builder session while allowing up to ten distinct accepted digests.
Both dev cases run after each accepted submission, `dev mean > 60` is recorded
without triggering freeze, and the six hidden cases remain post-freeze only.
Compatibility diagnostics preserve the older offline self-test without
restoring the retired fixed Candidate-1/Candidate-2 protocol. Result uses the
shared semantic judge and Code uses the evaluator-owned Create-judge wrapper.

Offline negative tests and the legacy self-test passed. A new real hidden smoke
with evaluator-owned broker and cleanup evidence remains required.

The current one-stop implementation also closes the startup cleanup gap: each
owned broker is tracked only after successful startup, partial startup removes
the failed/current container and all earlier starts, pilot mode omits the
unused Result-judge broker, and cleanup verifies every started container is
absent. Pilot summaries use the actual one-case hidden inventory. These are
provider-free changes; no smoke result is claimed.
