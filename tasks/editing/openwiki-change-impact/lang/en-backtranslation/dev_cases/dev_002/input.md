# Agent-loop public case 2: expired-owner recovery and conflicting retry

You are the OpenWiki lower agent under evaluation. Using the current request
and repository fixture, recover, through the real OpenWiki production CLI, a
request whose lease has expired and whose stage is incomplete. Re-verify
from the sources and the Git diff that the default separator of `label`
changed from a colon to an arrow, update only the affected pages, and
preserve the explicit-separator example, anchors, hand-written notes, and
unrelated pages. Clean up the untrusted partial stage, commit a recovered
receipt, and generate immutable publication and search state. Then handle a
conflicting retry of the same logical request with a different payload
digest: refuse it without changing documentation, receipt, publication, or
search state.

You must write `agent_result.json` and record the real tool/CLI trajectory,
dynamic observations, state/receipt bindings, and safety decisions in the
result. Do not read the evaluator, hidden cases, oracles, or credentials; do
not hard-code expected answers; do not pass off raw tool events as
model-authored text.

Then run coordinated impact maintenance on the attached CommonJS repository
in which `label(name, options = {})` keeps its signature but changes the
default separator from a colon to an arrow. The unified diff is the change
source.

The fixture starts with a durable claim for tenant `public-team`, request
`labels-002`, generation `2`, owned by a worker whose lease expired long
ago, plus incomplete staged files. Recover the processed request under a new
owner. Discard the untrusted incomplete staging, recompute from
sources/tests, repair only the default claim and example, and commit exactly
once with result `recovered`. Preserve the explicit-option example, anchors,
hand-written notes, and the unrelated parsing page.

After success, the public runner changes only the `payload_digest` and
retries the same request identity. Refuse that call as a conflict without
changing documentation, tenant generation, receipt bytes, or any other
state. It must terminate immediately with a failed transaction report rather
than waiting for the expired owner.

Select static site `public-handbook` at generation 2 under
`.openwiki-public` and search index `public-handbook` at generation 2 under
`.openwiki-search`, both with base path `/handbook/`. Reconcile the
untrusted partial publication/index, publish a self-contained local
`index.html`, make `Label defaults` searchable, and ensure the old
`colon defaults` claim returns no results.

Fixtures live in `assets/project/`; the runner materializes the diff, the
expired claim, the incomplete staging, and the conflicting retry. Use the
builder's production contract inputs.

Product contract: `agent_result.json` must be written at the working
directory root, and the same object must be emitted verbatim as the last
JSON value on the production CLI's stdout; authorship is confirmed by the
byte-for-byte equality of the two. Write it out as soon as the first product
response arrives and refresh it after every step; when stopping early, use
`decision.completion_claim = "partial"` and honestly list the parts not
attempted.
