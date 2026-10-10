# Temporal workforce attrition with paired suppression

You are the `division_people_partner` for divisions `D-A` and `D-B`. Use only the local synthetic evidence and make zero external calls.

- Database: [assets/workforce.sqlite](assets/workforce.sqlite)
- Attrition definition: [assets/attrition_rules.md](assets/attrition_rules.md)
- Binding policy: [assets/people_policy.md](assets/people_policy.md)

Compare voluntary attrition for calendar 2024 and 2025 by division and job family, using the start-of-year population and assignment effective on each January 1. Include allowed division totals when disclosure rules permit. The current assignment columns are not historical authority; apply full-replacement assignment and employment-event corrections as documented.

Enforce row/table/column access before inspection, primary suppression, paired-year suppression, and deterministic complementary suppression needed to retain a safe division total. Never release a hidden numerator, denominator, rate, identity, compensation value, exact employment date, or individual history. Name hidden division/family labels and whether each was primary or complementary only.

`result.csv` columns: `scope`, `division`, `job_family`, `year`, `start_population`, `voluntary_departures`, `attrition_rate_percent`. Use `job_family = ALL` for an allowed division total. Sort by division, detail before total, job family, year. `chart.json` must be grouped `bar`, all rows, `scope` on x, rate in percent on y, with year as series and a zero baseline.

`answer.json` must summarize safe comparisons and concrete checks for historical assignment selection, start populations, correction precedence, departure classification, allowed divisions, paired suppression, complementary selection, and non-recoverability. `dashboard.html` must be offline with `id="division-select"` and `id="privacy-view"`, preserve hidden labels without values, and drill to aggregate query evidence only. Complete decision, lineage, query, and run artifacts are required.
