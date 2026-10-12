# Product-family net revenue close: determine evidence sufficiency

Use only the local synthetic evidence. External research cannot provide internal allocation authority and is prohibited.

- Database: [assets/finance.sqlite](assets/finance.sqlite)
- Close policy: [assets/close_policy.md](assets/close_policy.md)

The finance director asks for consolidated net revenue in USD by product family for 2024 and 2025, the growth percentage, and the fastest-growing family, using the January 2026 approved close basis.

Determine from the supplied rules and actual key coverage whether that requested product-family result is supportable. Inspect enough aggregate schema/data evidence to distinguish an answerable calculation from a material unresolved allocation. Do not drop unmatched financial adjustments, spread them evenly, infer a family from unrelated ledger rows, use the decoy FX set, or publish a directional ranking from incomplete family values.

If the evidence is insufficient, produce the global structured `insufficient_information` success bundle. `answer.json` must identify the concrete affected authority/key coverage discovered from the data, quantify the blocker at a safe aggregate level, and state the smallest approved input needed to resolve it—without reporting family revenue, growth, or ranking. `queries.json` may contain read-only aggregate profiling evidence. `result.csv` must have the header `product_family,revenue_2024_usd,revenue_2025_usd,growth_percent` and zero data rows; `chart.json` must be `not_applicable`.

`dashboard.html` must be a self-contained blocked-state page with controls `id="blocker-scope"` and `id="evidence-view"`; it must not offer a publishable metric state. `decision.json` and `lineage.json` must trace the insufficiency and displayed aggregate blocker evidence to declared queries and supplied assets. Complete runtime reporting is required.

Role `finance_close_analyst` may access all supplied tables. The correct status depends on the evidence, not a blanket preference to refuse.
