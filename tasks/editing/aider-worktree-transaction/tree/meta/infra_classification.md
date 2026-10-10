# Infrastructure classification

`candidate_build_failure`: invalid/empty/unsafe patch, failed install/import/syntax, or missing Aider artifact. Candidate-owned; report Candidate 0/cap as specified.

`candidate_behavior_failure`: valid Aider lower process and successful broker calls, but wrong action, state, receipt, artifact, recovery or safety behavior.

`infrastructure-invalid`: broker/provider/credential/mount/Docker/runtime/evaluator failure, timeout before valid lower execution, corrupted evaluator state, or missing case contract. Report N/A/blocked and preserve evidence.

`static_smoke_only`: self-test or syntax/build smoke without real broker success. Never publish as Agent-loop Result.
