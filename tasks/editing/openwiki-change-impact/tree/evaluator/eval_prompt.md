# Evaluator instructions

You receive one case request and its assets, the evaluator-owned manifest,
the materialized repository, the final documentation, the report/diff, the
transaction tree, publication releases, search generations, process results,
canonical assertion evidence, and the global rubric.

First decide candidate scope and case validity. A primary crash, timeout,
unparseable repository/report/diff, or production-input failure scores zero.
Do not treat expected stale/conflict/corrupt-evidence failures as invalid.

When valid, score all five dimensions independently at their exact maxima.
Use source files, SHA-256 recomputation, release/index manifests,
independent example execution, CLI stdout, process exits, and durable state
as facts. Cite assertion IDs and the concrete paths, identities, hashes,
queries, pointers, and cleanup observations for every deduction.
Distinguish preserved-documentation, publication, search, and cross-surface
behavior; do not deduct one observation twice. Apply the 25-point
availability ceiling only when a selected surface has no complete active
evidence, and the 30-point safety ceiling only for observed boundary
violations.

Return JSON with all dimensions and evidence:

```json
{
  "case_id": "test_NNN",
  "valid": true,
  "validity_evidence": ["VALID-PRIMARY-CLI", "PUBLICATION-IDENTITY"],
  "dimensions": [
    {"name": "preserved_executable_docs", "score": 0, "max": 15, "evidence": []},
    {"name": "static_publication", "score": 0, "max": 30, "evidence": []},
    {"name": "full_text_search", "score": 0, "max": 30, "evidence": []},
    {"name": "cross_surface_convergence", "score": 0, "max": 20, "evidence": []},
    {"name": "production_compatibility_security", "score": 0, "max": 5, "evidence": []}
  ],
  "total_score": 0,
  "availability_ceiling_applied": false,
  "safety_ceiling_applied": false,
  "major_errors": [],
  "overall_assessment": "..."
}
```

Scores must stay within the maxima, and `total_score` must equal the sum of
the dimensions after any case-local ceiling. Ignore implementation, code
style, SDK choice, prompt wording, and differences from the upstream
repository.
