# A verifiable terminal state for the acceptance preview and an attestation that survives restart

In the current app's Acceptance mode, complete a user task that requires
first observing a failure, then completing a focused fix and a regression
confirmation. Use the app's existing chat, requirements ledger, generated
tests, repair budget, code evidence, and test-run records; do not treat a
chat completion or a button click as acceptance passing. The fix should be
focused: do not rewrite app files unrelated to the defect, and do not
modify, delete, skip, or weaken the acceptance target test itself.

After the fix, start the **exact** acceptance target from the product's
Preview/Tests workflow. The target is a product fact: first read the full
content of the current acceptance target from the product, then use it
verbatim in the start command; do not guess any of its fields from the
filename. During the run, observe the main-process-owned session state and
test progress; after completion, read the run record and test attestation
persisted by the product.

Verify for yourself rather than paraphrasing: whether the session, run
generation, test target, and workspace revision described by the attestation
agree with the one you started; whether the target fingerprint corresponds to
the real content of the target file when you started; whether, for a run in
which no drift occurred, the started, current, and finished revisions agree
with one another. If any of these does not match, it does not constitute
acceptance passing.

Then coordinate one `main`/database re-initialization and re-read the same
session and the same attestation in a fresh process. The recovered
attestation must be the same record, identical field by field, and the
target must not be re-executed because of the restart.

The result must bind the `appId`, `chatId`, Acceptance `runId`, session,
workspace revision, and target fingerprint returned by the product in this
run; if any binding is missing, do not claim acceptance passing. The result
artifact's `product_actions` must be the exact ordered record of the product
actions you actually dispatched, with nothing merged, omitted, or added after
the fact. All identifiers and digests must come from actual product returns
or durable records; write facts that cannot be verified into `blockers` with
the reason. Do not guess dynamic values, and do not write tests that do not
exist in order to reach a passing conclusion.

Before starting the preview, first read the current acceptance target from the
product (the run snapshot's `currentTarget`) and use it verbatim in the start
command. If the product does not publish it, or the published value lacks a
field, record that fact honestly in `blockers`; do not fill it in on the
product's behalf from a filename, a title, or any inference. `product_actions`
must be the exact ordered record of the product actions you actually
dispatched, including the final `finish`, with nothing merged, omitted, or
added after the fact.
