# Independent Code rubric — 100 points

The Code axis is evaluated from the frozen Candidate patch and production seams;
it never consumes Agent-loop Result scores or broker statistics.

| Dimension | Points |
|---|---:|
| `interface_lifecycle` | 15 |
| `requirement_mechanism_coverage` | 20 |
| `analysis_evidence_integrity` | 15 |
| `safety_privacy_side_effects` | 15 |
| `recovery_honest_failure` | 10 |
| `testability_observability` | 10 |
| `maintainability_generalization` | 10 |
| `resource_discipline` | 5 |

The Phase-A `code_runner.py` is a conservative structural precheck only. A
future independent Code judge may replace its heuristic with source review, but
the eight dimensions and weights remain locked.
