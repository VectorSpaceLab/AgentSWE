# Cancellation, a late callback, a duplicate stop, and a monotonic terminal state

In Acceptance mode, start an acceptance test that needs to be observed in
the Preview. Let the test enter a cancellable non-terminal phase, then issue a
stop through the product's existing Tests stop/cancel owner. The
cancellation must act on the test process that owns that app and run, and
leave a re-readable terminal fact.

After the stop, issue one more duplicate stop request and allow for a late
runner callback. Observe how the main process maintains the event sequence
and status monotonicity: late or duplicate notifications should not turn the
cancellation into a pass, restart the target, overwrite the receipt, or
create a second attestation; the event sequence must not go backwards; the
attestation identifier must be the same before and after the duplicate stop,
and its outcome must agree with the session's terminal state.

Read the session state separately before and after the duplicate stop, and
verify that the two reads describe the same session. Control receipts, the
run snapshot, test evidence, the repair budget, and unresolved items must
remain consistent; competing requests with a different operation identity or
a different generation should be clearly refused or classified.

Write the result artifact and bind the actual `appId`, `chatId`, `runId`,
session, revision at cancellation, and target fingerprint, distinguishing the
cancellation already observed, the setup/runner facts not yet completed, and
any infrastructure limits. The attestation, status, and event sequence must
come from real product surfaces. Do not guess that the test passed because
the last notification was not received, and do not treat an ordinary
diagnostic log as a cancellation receipt. `product_actions` must be the exact
ordered record of the actions actually dispatched.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command. If the product does not publish it, or the published value lacks a
field, record that fact honestly in `blockers`; do not fill it in on the
product's behalf from a filename, a title, or any inference. `product_actions`
must be the exact ordered record of the product actions you actually
dispatched, including the final `finish`, with nothing merged, omitted, or
added after the fact.
