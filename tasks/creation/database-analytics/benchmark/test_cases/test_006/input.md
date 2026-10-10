# Inventory stockout exposure from an authorized recovery snapshot

Use only the synthetic local assets; do not access the web.

- Database: [assets/inventory.sqlite](assets/inventory.sqlite)
- Recovery and valuation rules: [assets/inventory_rules.md](assets/inventory_rules.md)
- Authorized fallback costs: [assets/fallback_costs.csv](assets/fallback_costs.csv)

For 2026-06-30, calculate stockout replacement-cost exposure by active warehouse and product category. Identify the three warehouses with greatest total exposure and their leading category. Include all categories for each selected warehouse, including zero-exposure categories.

The curated source has stale complete data and an incomplete failed load for the report date. Follow the whole-snapshot source hierarchy rather than mixing rows. Deduplicate raw inventory and demand revisions, cross active warehouses with active products before joins, treat an absent raw row as zero on hand, apply the cost hierarchy and locked FX set, and preserve unrounded values through ranking and contribution calculations. Show numeric checks for snapshot control, raw/demand corrections, missing inventory coverage, cost fallback effective-date selection, missing cost mappings, currency/minor units, fan-out, and category-to-warehouse reconciliation.

`result.csv` columns: `warehouse`, `category`, `units_short`, `exposure_usd`, `warehouse_contribution_percent`; sort by warehouse total exposure descending, category exposure descending, category. `chart.json`: `stacked_bar`, every row, warehouse on x, exposure in USD on y, category as series, zero baseline.

`answer.json` must document the selected source, name the top three warehouses and leading category, and disclose any unresolved mappings. `dashboard.html` must be offline with `id="warehouse-select"` and `id="source-view"`, identify curated versus recovered evidence, synchronize rows/totals, and drill to query plus SQLite/CSV lineage. Complete decision, lineage, query CSV, and runtime artifacts are required.

Role `supply_chain_analyst` may access all supplied inventory assets.
