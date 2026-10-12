# Public dev 001 — persistent misconception to verified review plan

A learner repeatedly applies the same invalid operation while working through a
small algebraic transformation. Use DeepTutor's active mastery path rather than
guessing from the prose alone.

Inspect the current objective and recorded evidence, continue diagnosis only if
needed, and establish one bounded remediation with a future review. Then inspect
the learner portfolio snapshot and its provenance. Use the public remediation,
review, snapshot, attestation, witness, verification, and chain-audit
interfaces when relevant. The complete surface is documented in
`input/05_mastery_tool_contract.md`; the product must register those interfaces
rather than leaving the agent with only the original five mastery tools. Ground
the recommendation in the current accepted state.

Finish with the required JSON artifact. It must identify the learner's next safe
action, cite actual tool-returned IDs/revisions/digests rather than invented
values, distinguish verified from merely observed evidence, and say what should
happen if a later client presents a stale chain head. Do not reveal raw answers
or claim that an unverified restore succeeded. Return the artifact as one JSON
object with `schema_version`, `case_id`, `status`, `summary`, and `artifacts`;
the product must persist the exact model-authored object when its artifact
output path is enabled.
