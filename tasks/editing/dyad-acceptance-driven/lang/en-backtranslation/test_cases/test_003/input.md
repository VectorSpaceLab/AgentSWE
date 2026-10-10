# Non-disclosure of denials and an auditable denial ledger

In Acceptance mode, execute an app task that requires Preview, Tests, and
durable run control. First establish a run record through the product's
normal chat entry and start one authorized preview, then inspect its
lifecycle through the real typed control and read interfaces.

Consider three kinds of invalid request separately: a control command from a
stale generation, a control command from another app or chat, and a read
request without authority. Invalid requests must not start tests, change the
current run, increase the repair budget, overwrite existing evidence, or
disclose the internal data of another session/run/attestation. Read the
authorized run snapshot once before and once after issuing these requests
and compare field by field: apart from the auditable ledger the product uses
to record denials, there should be no other difference; the ledger entries
themselves should be bounded facts rather than the content of the denied
request carried back verbatim.

Clearly distinguish "denied by the product" from "bypassed by you". If a
request is denied, record the denial itself as the result honestly; do not
switch identities, use another channel, or probe repeatedly until it
succeeds. If some fact cannot be obtained through an authorized read, mark
the evidence as missing explicitly rather than filling it in by formal
inference. Authority cannot be inferred from the shape of an ID, and a denial
cannot be packaged as success.

The result artifact must bind the authorized session's `appId`, `chatId`,
`runId`, session, revision, and target fingerprint, explicitly list how the
three denied requests differ from that binding, the classification and reason
given by the product, and the comparison conclusion of the authorized
snapshot before and after the denials. `product_actions` must be the exact
ordered record of the actions actually dispatched.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command. If the product does not publish it, or the published value lacks a
field, record that fact honestly in `blockers`; do not fill it in on the
product's behalf from a filename, a title, or any inference. `product_actions`
must be the exact ordered record of the product actions you actually
dispatched, including the final `finish`, with nothing merged, omitted, or
added after the fact.
