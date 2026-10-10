# Public case 001: lost-response retry, a verifiable terminal state, and an attestation that survives restart

In Acceptance mode, complete the registration flow of the current app.

Acceptance criteria:

- A new valid email registers exactly once and reaches the dashboard.
- The authenticated session survives a page reload without registering
  again.
- Duplicate and syntactically invalid email attempts are still rejected and
  expose accessible field errors without creating a session.
- The behavior is covered by a generated Playwright target that first
  observes the seeded failure and then turns to passing after a bounded fix
  and regression. The fix should be focused and must not modify, delete,
  skip, or weaken that target test.

After the focused fix, start that target with **Run in preview**. The target
in the start command is a product fact: first read the full content of the
current acceptance target from the product (including its filter field),
then use it verbatim; do not guess from the filename. The first start
response is treated as lost, so retry a **byte-identical** command with the
same caller-stable operation identity and recover the same durable preview
session instead of starting another run.

Show setup/running/terminal progress and create an immutable passing
attestation. Verify for yourself rather than paraphrasing: whether the target
fingerprint corresponds to the real content of the target file at start;
whether, for a run without drift, the started, current, and finished
revisions agree with one another; whether the session, run generation, and
target described by the attestation are the one you started.

Then re-initialize the database/main-owned services and re-read the same
session and the same attestation in a fresh process: the recovered
attestation must be the same record, identical field by field, and the
target must not be re-executed because of the restart. The acceptance ledger
may use the attestation as focused passing evidence, but the attestation
itself does not replace the requirements, mutation failure, code links,
repair budget, or the Cycle 001 control receipts.

The result artifact's `product_actions` must be the exact ordered record of
the product actions you actually dispatched. Use the public fixture selected
by `dev_cases/run_dev_case.py --case dev_001`. Do not use live network
services.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command; if the product does not publish it, or publishes it incompletely,
record that fact honestly in `blockers`. `product_actions` must be the exact
ordered record of the actions actually dispatched, including the final
`finish`.

Public-round feedback returns, item by item, the seven checks
(`contract:*`) of the "Independently verifiable product contract" in
`input/02`, which you can use to calibrate the implementation.
