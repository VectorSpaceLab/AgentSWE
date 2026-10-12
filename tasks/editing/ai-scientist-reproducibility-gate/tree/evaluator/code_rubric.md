# AI Scientist Edit Code rubric (100 points)

| Dimension | Points | Required evidence |
|---|---:|---|
| interface_lifecycle | 15 | Governed verification/release entry remains reachable through the real AI Scientist launcher and supports status, recovery, and cancellation. |
| requirement_mechanism_coverage | 20 | Claim evidence, capsule, budget, attestation, notification, and provenance mechanisms are implemented coherently. |
| analysis_evidence_integrity | 15 | Decisions bind to immutable manifests, artifact digests, worker evidence, generations, and receipts. |
| safety_privacy_side_effects | 15 | Project/tenant isolation, authorization, redaction, and exactly-once external effects are enforced. |
| recovery_honest_failure | 10 | Response loss, partial workers, corruption, drift, and stale generations recover or fail honestly. |
| testability_observability | 10 | Focused tests and bounded diagnostics expose lifecycle and provenance invariants without leaking private oracle data. |
| maintainability_generalization | 10 | The implementation is cohesive, product-native, and generalizes beyond the supplied fixtures. |
| resource_discipline | 5 | Retries, journal/capsule storage, worker cleanup, and model/tool use are bounded. |

Score each dimension independently. Cite exact Candidate paths and line ranges. Do not use hidden outcomes or Result scores as Code evidence.
