# Shipment SLA deterioration and delay-cause contribution

Analyze only the local synthetic assets; external search/retrieval is prohibited.

- Database: [assets/logistics.sqlite](assets/logistics.sqlite)
- SLA rules: [assets/sla_rules.md](assets/sla_rules.md)

Compare June 2026 with July 2026 SLA breach rate for each carrier/service-level pair having at least 40 eligible shipments in each month. Select the two pairs with the largest percentage-point increase. For each selected pair, decompose July positive delay hours by primary delay cause and identify the leading cause.

Use origin-local acceptance month, corrected delivery and exception events, effective/revised commitment rules, and total shipment weight converted to kg before weight-band selection. Qualify at shipment grain before joining packages or exception events. Demonstrate checks for local-month boundaries, package fan-out, kg/lb conversion and the 25 kg threshold, commitment version/effective-date selection, corrected milestones, voided exceptions, exactly one primary cause, eligibility exclusions, breach-rate denominators, and cause-hours reconciliation.

`result.csv` columns: `carrier`, `service_level`, `delay_cause`, `june_shipments`, `june_breach_rate_percent`, `july_shipments`, `july_breach_rate_percent`, `change_percentage_points`, `july_delay_hours`. Include each July cause with positive delay for the selected pairs; repeated pair-level values must agree. Sort by pair change descending, carrier, service level, cause delay descending, cause. `chart.json` must be `stacked_bar`, all rows, a combined carrier/service scope on x (you may use `carrier` plus `service_level` encodings), `july_delay_hours` on y in hours, and `delay_cause` as series, from zero.

`answer.json` must name the selected pairs, changes, July total delay hours, and leading cause. `dashboard.html` must be offline with `id="carrier-service-select"` and `id="rule-version"`, keep rows/totals synchronized, and provide provenance. Complete decision, lineage, queries, and runtime report are required.

Role `network_performance_analyst` may use all analytical tables except `recipient_directory`; never inspect or expose recipient data.
