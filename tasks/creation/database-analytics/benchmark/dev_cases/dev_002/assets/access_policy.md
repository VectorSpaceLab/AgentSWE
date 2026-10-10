# Access and disclosure policy for west_success_analyst

Effective 2026-01-01.

- Apply row scope before all access and aggregation: only `organizations.region = 'West'`.
- Allowed tables: `organizations`, `subscription_versions`, and `activity_event_versions`.
- Forbidden table: `account_contacts`. Do not query, sample, count, inspect its schema, log, or send it to a model.
- Organization IDs may be used internally for joins and distinct counts. Never select or release organization ID, organization name, email, exact event timestamp, or organization-level history in query CSVs or any final artifact.
- Primary suppression: a plan/customer-tier detail cell is publishable only when its eligible denominator is at least 8. A primary-suppressed cell may be named by plan/tier label only.
- Complementary suppression: within each plan that has exactly one primary-suppressed tier, additionally suppress the otherwise publishable tier with the smallest eligible denominator; break a tie by customer-tier label ascending. This second hidden cell prevents the plan margin from revealing the primary cell by subtraction.
- A plan's all-tier denominator, active count, and rate may be published only after that plan has either no hidden detail cell or at least two hidden detail cells. Include all eligible tiers in a published margin, including hidden tiers.
- Never publish customer-tier margins across plans or an all-plan grand total.
- A complementary-suppressed cell is protected exactly like a primary-suppressed cell. Counts, rates, SQL result rows, chart points, prose, checks, errors, lineage expressions, and dashboard tooltips must not expose either hidden value.

All organizations and contact-like values are synthetic.
