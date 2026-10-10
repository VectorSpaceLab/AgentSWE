# DST-aware charging energy and tariff cost

Use only the closed synthetic assets; do not search or retrieve external material.

- Database: [assets/charging.sqlite](assets/charging.sqlite)
- Metering and tariff rules: [assets/metering_rules.md](assets/metering_rules.md)

For local service dates 2026-03-07 through 2026-03-10 inclusive, calculate delivered energy and energy cost by site/local date. Identify each site's peak-energy day, its four-day energy and cost totals, and whether daily rows reconcile to those totals.

The data spans offset changes and local midnight, cumulative meters, stale negative revisions corrected by later rows, multiple reading kinds, canceled sessions, revised effective tariffs, and misleading estimates/current offsets. Show numeric checks for latest-revision selection, retained negative deltas, completed-session exclusions, UTC-versus-local date differences, one tariff per session, join fan-out, Wh/kWh and cents/USD conversion, and daily-to-total reconciliation.

`result.csv` columns: `site`, `local_service_date`, `energy_kwh`, `cost_usd`, `is_site_peak_day`; include every site/date combination in scope, using zero where a site has no eligible session. Sort by site/date. Display energy to three decimals and cost to two decimals. `chart.json`: `line`, all rows, local date on x, energy in kWh on y, site as series, no dual axis.

`answer.json` must name each peak day and four-day totals. `dashboard.html` must be offline with controls `id="site-select"` and `id="tariff-view"`, show anomaly checks, and trace displayed site/day values to queries and sources. Complete decision, lineage, query CSV, and runtime artifacts are required.

Role `fleet_energy_analyst` may use all tables except `driver_accounts`; do not inspect that table or expose driver IDs.
