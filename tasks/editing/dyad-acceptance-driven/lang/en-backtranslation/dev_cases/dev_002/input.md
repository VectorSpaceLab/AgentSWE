# Public case 002: in-run drift invalidation, byte restoration, and cancellation

In Acceptance mode, implement the inventory CSV workflow for the current app.

Acceptance criteria:

- Import accepts quoted commas/quotes, rejects duplicate SKUs and invalid
  quantities by original row number, and commits only valid unique rows.
- Category, active-status, and minimum-quantity filters can be combined.
- Export contains the complete filtered set across all pages with stable
  CSV escaping; a generation failure shows an error, does not claim a
  successful export, and does not corrupt current data.
- The generated behavior test first observes the seeded failure before the
  fix and must not be deleted, skipped, weakened, or replaced with unrelated
  content.

Run the focused target in the preview and expose durable live progress.
Before starting, first read the full content of the current acceptance
target from the product and use it verbatim. While the test is in progress,
produce one content change in the app workspace, then restore the bytes to
the original. Even if the runner reports the test as passing, the session and
its single terminal attestation must be classified by the product's rules,
and it must be possible to explain why the finished revision differs from
the started revision when drift occurred, why the already-invalidated
evidence does not come back to life after restoration, and why the target is
neither re-executed nor a second attestation produced. The invalidation
reason should be a bounded fact without absolute paths or log dumps.

Also exercise "Stop" during a separate preview that is still in
setup/running. Stop must use the existing Tests cancel owner, persist
`cancelled`, ignore the late terminal callback, and remain readable after
the renderer remounts; a duplicate stop must return the same attestation
rather than a second one. Ordinary non-acceptance test-panel runs must keep
their existing result and event behavior.

The result artifact's `product_actions` must be the exact ordered record of
the product actions you actually dispatched. Use the public fixture selected
by `dev_cases/run_dev_case.py --case dev_002`. Do not use live network
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
