# Privacy-safe adoption matrix with complementary suppression

You are the `west_success_analyst` preparing an adoption view as of 2026-06-30. Use only the supplied local evidence; external search and retrieval are prohibited.

## Assets and authority

- Database: [assets/adoption.sqlite](assets/adoption.sqlite)
- Metric history: [assets/adoption_definition.md](assets/adoption_definition.md)
- Binding permission and release policy: [assets/access_policy.md](assets/access_policy.md)

The access policy controls before metric calculation. Do not inspect a forbidden table or release a prohibited field even for diagnostics.

## Request

Calculate active-organization rates by subscription plan and customer tier within the permitted population. Also publish each plan's all-tier margin where the policy permits it. Apply the definition version effective on the report date, including latest subscription and event revisions, subscription validity, qualifying event types, distinct local activity days, distinct feature families, and exclusions.

Apply primary suppression and the policy's deterministic complementary suppression before releasing any value. The goal is to retain safe plan margins without making a primary-suppressed tier cell recoverable. State primary- and complementary-suppressed plan/tier labels separately, but do not disclose their counts, rates, identities, or histories. Do not publish customer-tier margins or a grand total.

Show concrete checks for role row scope, forbidden-table avoidance, subscription/event revisions, distinct-day and feature-family logic, primary thresholding, complementary selection, and non-recoverability from each published plan margin.

## Output

- `result.csv`: disclosure-safe detail rows plus allowed plan margins. Sort by `plan`, then detail before total, then `customer_tier`. Columns: `scope`, `plan`, `customer_tier`, `eligible_organizations`, `active_organizations`, `active_rate_percent`. Use `customer_tier = ALL` for a plan margin and a readable `scope` such as `Core / SMB` or `Core / ALL`.
- `chart.json`: `bar`, every result row, `scope` on x and `active_rate_percent` in percent on y from zero.
- `answer.json`: summarize released rates and plan margins; list primary and complementary labels only; record every applied privacy rule.
- `dashboard.html`: self-contained and offline with a working `id="plan-select"` control that filters released rows and an `id="suppression-notes"` control that reveals the distinction between primary and complementary suppression without revealing hidden values. Provide row-level provenance to declared aggregate queries.
- `decision.json`, `lineage.json`, query CSV evidence, and `run_report.json` under the global contract.
