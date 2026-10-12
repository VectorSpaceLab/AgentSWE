# Dyad Agent-loop execution environment

These runner-side helpers stay outside input/repository, so the pinned Dyad
source is never changed by a case run.

headless_chat_flow.py runs a temporary happy-dom Vitest test against the
Candidate's own source. It uses the hybrid harness to register the production
IPC host, then exercises the real chat:stream handler, SQLite database, Git
checkout, response processor, durable messages, and any Candidate-provided
Acceptance Preview/session/attestation handlers. The Electron mock and
auxiliary fake endpoints are listed as test doubles; they are not evidence of
native Electron execution or provider success.

Result classes:

- headless_real_acceptance: reserved for a Candidate that exposes and
  persists the acceptance Preview/Attestation surface.
- headless_real_chat_stream: real Dyad chat flow was observed, but the
  acceptance Preview/Attestation chain was unavailable or incomplete.
- native_electron_exited / native_electron_failed: npm start was invoked; an
  exit code alone is never a success claim.
- mock_or_stub_only: no real product evidence; never count this as success.

The evaluator case wrapper separately records broker calls, successful calls,
provider failures, Acceptance-surface presence, and complete attestation. A
real chat path with a missing Acceptance surface is Candidate evidence but not
a valid Acceptance result. A provider failure is infrastructure-invalid.
