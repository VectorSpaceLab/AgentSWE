# Infrastructure classification

`candidate_invalid`: missing/unsafe/unappliable patch, forbidden changed path,
candidate materialization/build/type/contract failure, or a valid lower product
that intentionally makes zero model calls.

`infrastructure_invalid`: broker/provider outage, credential preflight failure,
Docker/runtime mount failure, evaluator-owned case-service failure, missing
result from the evaluator, or resource failure attributable to the harness.

`valid_behavior`: the patched OpenHands lower process ran, made broker calls,
produced a contract artifact, and the evaluator observed the required product
state. A low score here is still a Candidate behavior result, not N/A.

No static self-test, syntax smoke, native Vitest score, or empty broker stats may
be reported as a successful Agent-loop Result.
