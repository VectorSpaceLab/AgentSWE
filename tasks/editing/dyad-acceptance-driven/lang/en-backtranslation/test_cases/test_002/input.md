# Idempotent retry after a lost response and an operation-identity conflict

In Acceptance mode, complete one acceptance task provided by the current app
and use the user-visible preview execution path. When starting the preview,
allow for the first response being lost: retry the **byte-identical**
original command with the same caller-stable operation identity and recover
the existing session from the product's durable state. The retry should not
quietly create a second test process, a second run, or a second attestation.

After confirming that the session has been recovered, issue one start
command with **different content** under the same operation identity, as a
probe of the caller-identity contract. Observe how the product handles it and
record honestly its classification, its effect on the existing session
(whether the session identity, status, or event sequence changed), and
whether it caused any additional test process or attestation. Do not use the
probe as a way to continue the original command, and do not try to bypass
the product's ruling.

Observe and record the durable progress of the queued, setup, running, and
terminal phases, distinguishing renderer notifications from the
main-process authoritative state. Report item by item this run's `appId`,
`chatId`, `runId`, session, revision, and target fingerprint, and these
values must remain consistent before and after the retry and before and
after the probe.

The result artifact must state: the actual number of test-process starts,
the retry operation, the product's ruling on the conflict probe, the
terminal status, the attestation identifier and readability, and any facts
that could not be verified. `product_actions` must be the exact ordered
record of the actions actually dispatched. Do not substitute non-empty
strings for real receipt, run, or attestation data, and do not write the
number of starts as an unobserved guess.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command. If the product does not publish it, or the published value lacks a
field, record that fact honestly in `blockers`; do not fill it in on the
product's behalf from a filename, a title, or any inference. `product_actions`
must be the exact ordered record of the product actions you actually
dispatched, including the final `finish`, with nothing merged, omitted, or
added after the fact.
