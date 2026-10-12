# Honest reconciliation after a cold restart and ordinary-mode compatibility

In Acceptance mode, complete an app task that includes generated tests, a
focused fix, a regression run, and durable evidence. First establish the run,
the control receipts, and one preview in progress, and while that preview is
still in a non-terminal phase, coordinate one `main`/database
re-initialization. The recovery read must rely on the product's durable
storage, not on in-process caches or renderer memory.

Interrupted queued/setup/running work must not remain in a non-terminal
state after recovery, must not be silently marked as successful, and must
not be re-run automatically without an explicit user action: verify which
terminal state with a bounded reason the product reconciled it into, whether
the attestation's outcome agrees with that terminal state, and whether the
target was indeed not executed again. Also verify that the identity of the
same run did not change across the restart, while the generation and control
sequence did not go backwards.

Then conduct one ordinary (non-Acceptance) chat in the same app and read the
same run record once before and once after it, verifying that ordinary mode
indeed changed no byte of the Acceptance evidence. Record the compatibility
observation separately from the Acceptance recovery result.

The artifacts before and after recovery must re-bind the same `appId`,
`chatId`, `runId`, session, workspace revision, and target fingerprint using
durable reads; chat text or in-process objects cannot substitute for these
values. The result artifact should list the facts actually read before and
after the restart, the reconciliation conclusion and reason, the
ordinary-mode check, the attestation status, and a safe next step; do not
substitute a chat completion event, an empty test count, or a self-composed
recovery summary for durable evidence. `product_actions` must be the exact
ordered record of the actions actually dispatched.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command. If the product does not publish it, or the published value lacks a
field, record that fact honestly in `blockers`; do not fill it in on the
product's behalf from a filename, a title, or any inference. `product_actions`
must be the exact ordered record of the product actions you actually
dispatched, including the final `finish`, with nothing merged, omitted, or
added after the fact.
