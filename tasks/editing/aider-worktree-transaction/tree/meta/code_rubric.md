# Independent Code rubric

The Code axis is scored independently from Agent-loop Result. Fixed dimensions and weights:

| dimension | weight |
|---|---:|
| interface_lifecycle | 15 |
| requirement_mechanism_coverage | 20 |
| analysis_evidence_integrity | 15 |
| safety_privacy_side_effects | 15 |
| recovery_honest_failure | 10 |
| testability_observability | 10 |
| maintainability_generalization | 10 |
| resource_discipline | 5 |

The runner accepts an evaluator-owned JSON contract with one integer score per dimension, verifies the fixed weights and total, and never uses the lower-agent Result as a substitute.
