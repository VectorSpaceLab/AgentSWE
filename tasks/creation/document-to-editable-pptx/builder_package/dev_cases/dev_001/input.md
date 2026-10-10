# Presentation Request: Metrovale Fleet-Charging Pilot Review

Create an 8-slide, 16:9 decision deck for the Metrovale municipal fleet steering committee. This is a **closed-corpus** assignment: use only the supplied files, and make zero web search or retrieval calls.

## Decision

The committee must decide whether to approve a six-month controlled expansion of the depot charging pilot. The evidence supports improvement, but the deck must not imply that reliability or cost targets have already been met.

## Sources and Priority

Read all supplied files:

- [pilot_monthly.csv](assets/pilot_monthly.csv)
- [operations_memo.md](assets/operations_memo.md)
- [measurement_rules.md](assets/measurement_rules.md)
- [brand_mark.svg](assets/brand_mark.svg)

When figures conflict, `measurement_rules.md` controls definitions and calculations, the CSV controls monthly values, and the memo supplies qualitative context only. Surface the memo's conflicting uptime statement rather than silently repeating or deleting it.

## Required Analysis

- Recompute monthly and whole-period successful-session rate.
- Recompute whole-period uptime using the defined denominator.
- Recompute cost per successful session for the first and last three months and show the change.
- Identify the two largest operational constraints supported by the sources.
- Do not create a forecast. State what the next six months must test.

## Required Story

1. Decision and evidence-bound status.
2. Three-point executive readout.
3. Demand and successful sessions over time in one native editable chart.
4. Reliability and the uptime-definition conflict.
5. Cost efficiency, with formula and periods visible.
6. Editable causal diagram from constraints to service effect.
7. Controlled-expansion plan with owner, measure, threshold, and stop condition.
8. The exact approval requested and two unresolved questions.

## Design and Delivery

- Use the supplied mark on the cover and closing slide without changing its proportions.
- Use a white canvas, `#17324D` navy, `#00A6A6` teal, `#F2B134` amber, and dark gray. Amber is for unresolved risk, not decoration.
- Use a 12-column grid, 0.55-inch outer margins, slide numbers at bottom right, and no rounded cards.
- Add compact `[S#]` citations on every factual slide and resolve them in `source_manifest.json`.
- Put concise methodology speaker notes on slides 3, 4, and 5.
- Add meaningful alt text to the logo, chart, and causal diagram; never rely on color alone.
- Keep body text at 18 pt or larger and citation text at 9 pt or larger.
- Do not use photographs, decorative gradients, invented quotations, or unsupported causal claims.

The deck, source manifest, and run report must satisfy the global delivery contract.
