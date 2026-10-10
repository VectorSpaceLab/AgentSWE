# In-run workspace drift and permanent invalidation of the attestation

In Acceptance mode, complete an app task that generates a Playwright
acceptance target and, after the fix is complete, start that target from the
Preview/Tests surface. Before the run starts, record the workspace revision
and target fingerprint computed by the product; during the run, produce one
content change only through local workspace operations allowed by the task,
then observe how the main process reconciles that change with the test's
terminal state, and finally restore the bytes to the original and observe
again.

Even if the runner returns a passing test, acceptance must not pass: verify
whether the product handles protected-byte drift during execution separately
from the runner result, how the session, run, and attestation are each
classified, and whether the classification reason is a bounded fact rather
than a log dump or an absolute path. Also verify whether the revision fields
themselves really change with the content: the started and finished
revisions should differ when drift occurred and agree when no drift
occurred.

After the bytes are restored, the already-invalidated evidence must keep its
historical facts, must not come back to life automatically, and the
restoration must neither re-execute the target nor produce a second
attestation.

The result artifact must record, at the same time, `appId`, `chatId`,
Acceptance `runId`, session, started revision, finished revision, target
fingerprint, drift observations, terminal status, invalidation reason,
attestation outcome, and the cross-surface acceptance gate status. Merely
writing "passed" or copying an earlier revision digest is not enough to
complete the task; when it cannot be confirmed that the current workspace
matches the target, keep the uncertainty and give a safe next step.
`product_actions` must be the exact ordered record of the actions actually
dispatched.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command. If the product does not publish it, or the published value lacks a
field, record that fact honestly in `blockers`; do not fill it in on the
product's behalf from a filename, a title, or any inference. `product_actions`
must be the exact ordered record of the product actions you actually
dispatched, including the final `finish`, with nothing merged, omitted, or
added after the fact.
