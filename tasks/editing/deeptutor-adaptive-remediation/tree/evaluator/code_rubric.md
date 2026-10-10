# Independent Code rubric (100 points)

Score the submitted patch and its engineering evidence independently of all
Agent-loop Result files, broker scores, and model trajectories.

| Dimension | Points |
|---|---:|
| interface_lifecycle | 15 |
| requirement_mechanism_coverage | 20 |
| analysis_evidence_integrity | 15 |
| safety_privacy_side_effects | 15 |
| recovery_honest_failure | 10 |
| testability_observability | 10 |
| maintainability_generalization | 10 |
| resource_discipline | 5 |
| **Total** | **100** |

The judge cites patch paths and static/build evidence for each dimension. It
must not reward implementation similarity to DeepTutor or use a behavioral
Result as a proxy for code quality.
