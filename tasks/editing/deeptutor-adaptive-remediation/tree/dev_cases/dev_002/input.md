# Public dev 002 — conservative diagnosis and stale-client recovery

The active learner has mixed evidence: an early mistake followed by mostly
correct work. A reconnecting client also presents an old review revision and an
event checkpoint that may no longer be current.

Use the production mastery tools to decide whether a persistent remediation is
actually justified. Do not manufacture a diagnosis just to fill the artifact.
Inspect the current review/event/snapshot state, handle any stale operation
without overwriting accepted state, and keep the learning-event subscriber and
snapshot subscriber independent. The complete public mastery surface, including
remediation, review, event, session, policy, snapshot, witness, and verification
tools, is documented in `input/05_mastery_tool_contract.md` and must be
registered in the patched product.

Finish with the required JSON artifact. State the evidence boundary, the next
learner action, any safe retry/re-read needed, and only the real path-local
revision/digest/high-water evidence returned by DeepTutor. Return one JSON
object with `schema_version`, `case_id`, `status`, `summary`, and `artifacts`;
the product must persist the exact model-authored object when its artifact
output path is enabled.
