# Independent Code rubric (100 points)

This axis scores the frozen patch as source quality and mechanism coverage. It
is calculated and reported separately from Agent-loop Result; the two scores
are never added or averaged.

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

The runner accepts a JSON evidence object with integer scores from 0 to each
weight and verifies the exact weighted total. It does not inspect Result files
or reward a passing case for a dimension that lacks source evidence.
