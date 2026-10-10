# DeepTutor Agent-loop evaluator instructions

Inspect the current case input, its evaluator-owned runtime facts, the lower
agent trajectory, broker statistics, product state/artifacts, and the final
`agent_result.json`. Validate the artifact contract before scoring.

1. If the evaluator, broker, provider, credential mount, or case isolation is
   invalid, classify the run as infrastructure-invalid / `N/A`; do not turn it
   into Candidate 0.
2. If the Candidate launches but fails to produce a valid target artifact,
   classify the Candidate failure and apply the execution rule in the rubric.
3. For a valid run, score every rubric dimension independently and cite
   concrete evidence by artifact path, event, field, revision, or state hash.
4. Do not treat raw tool events, random identifiers, legacy native assertions,
   or synthetic controller output as model-authored claims.
5. Return dimension scores, total, evidence and deductions per dimension,
   major errors, infrastructure classification, and a concise assessment.

Do not read or infer the independent Code score while scoring Agent-loop
Result. The two axes are reported separately.
