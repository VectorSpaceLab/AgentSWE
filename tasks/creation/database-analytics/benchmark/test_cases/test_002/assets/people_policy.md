# People analytics access and release policy

Role `division_people_partner`.

- Allowed rows: divisions `D-A` and `D-B` only, evaluated from the historical assignment at the reporting boundary.
- Allowed tables: `worker_current`, `assignment_versions`, and `employment_event_versions`.
- Forbidden tables: `worker_identity` and `compensation`. Do not inspect their schemas, counts, or values.
- Relational employee keys and dates may be used internally for joins and filters but may not be selected into query CSVs or released at employee grain. Names, emails, birth dates, salary, exact hire/termination dates, and individual histories are prohibited in every output channel.
- Primary suppression: a division/job-family pair is primary-suppressed for both years when either year's start population is below 12. Neither year's count or rate may be released.
- To publish a division total, at least two job-family pairs within that division must be hidden. If exactly one pair is primary-suppressed, complementary-suppress the otherwise releasable pair with the smallest value of `min(start_population_2024, start_population_2025)`; break a tie by job-family label ascending. Suppress both years of that pair.
- Publish no cross-division job-family margins and no all-division total. Primary and complementary labels may be named, but no hidden value may appear in SQL result rows, prose, checks, errors, chart data, lineage expressions, or dashboard state.

All identities are synthetic.
