# Presentation Request: Apple FY2024 Filing and Earnings Briefing

Create a 9-slide, 16:9 analyst briefing for an investment committee reviewing Apple Inc.'s fiscal 2024 performance. This is an evidence and calculation exercise, not investment advice. Use only public sources published on or before 2024-11-15.

## Source Strategy and Recovery

Use Apple's fiscal 2024 Form 10-K as the controlling source for audited annual figures and accounting definitions. The preferred human interface is the SEC Inline XBRL viewer for accession `0000320193-24-000123`, but it is blocked for this case's automated body extraction. Record that viewer as an unavailable lead and recover from the official investor-hosted filed 10-K PDF, the filing's public SEC Archives document package when retrievable, and Apple's official FY2024 fourth-quarter results materials. Do not downgrade to an unsourced finance blog because the preferred interface is blocked.

Source priority is:

1. Audited Form 10-K financial statements and notes.
2. Apple's official earnings release and consolidated statements.
3. Official prepared remarks for management framing only.
4. Reputable secondary coverage only to identify a question, never to override the filing.

If annual and quarterly materials use different periods or definitions, label them and do not combine them silently.

## Required Analysis

Use fiscal years 2022, 2023, and 2024 where the filing supplies comparable values. Recompute and audit:

- Fiscal 2024 year-over-year net-sales growth.
- Fiscal 2024 gross margin and operating margin.
- Services share of fiscal 2024 net sales and Services year-over-year growth.
- Products versus Services gross-margin profile where the filing provides the inputs.
- Diluted EPS change from fiscal 2023 to fiscal 2024.
- At least one cash-return measure computed from cash-flow or equity disclosures, with the chosen denominator labeled.

Store formulas, source inputs, unrounded results, display rounding, and source IDs in the manifest. Use the filing's fiscal-year labels; do not call them calendar years.

## Required Story

1. Decision-oriented title and one-sentence thesis.
2. Executive scorecard: growth, mix, margins, EPS, and cash return.
3. Three-year net sales and operating income in an editable analytical chart.
4. Product/Services mix and growth, with definitions.
5. Gross-margin structure and the difference between consolidated and segment gross margin.
6. Geographic or reportable-segment signal selected for decision relevance; explain the chart choice.
7. Cash generation and shareholder returns, with a visible formula callout.
8. Risks and accounting/measurement caveats grounded in the filing, not generic technology risks.
9. Three committee questions and a balanced conclusion; no buy/sell recommendation or price target.

## Design and Delivery

- Use a white canvas, black, graphite, emerald `#11845B`, and vermilion `#D9483B`. No gradients, product glamour photography, or Apple logo.
- Use native editable charts with embedded data for all quantitative graphics and native tables for scorecards.
- Cite every number and filing-derived assertion on-slide using `[S#]`; map all material calculations in `source_manifest.json`.
- Put calculation and definition notes in the actual speaker notes on slides 2 through 7.
- Add meaningful alt text to every chart and table; label data directly and never rely on red/green alone.
- Use 0.55-inch margins, a maximum of two analytical regions per slide, body text at least 17 pt, and source lines at least 9 pt.
- Do not use trailing-twelve-month data, analyst consensus, current market prices, invented estimates, or non-GAAP measures not reconciled by the company.

The deck, source manifest, and run report must satisfy the global delivery contract.
