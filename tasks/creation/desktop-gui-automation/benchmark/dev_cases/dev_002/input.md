# Dispatch Cold-Chain Exceptions

## Case

- Case ID: `dev_002`
- Application: [Northline Dispatch Queue](assets/serve.py)
- Viewport: 1440 x 850 pixels
- Corpus: closed; use only the local application.

## Goal

In the **West** region, find all open `Cold chain` exceptions whose temperature is above `8.0 C`, including records beyond the first page. Select exactly shipments `NC-204`, `NC-219`, and `NC-233`. Add scoped bulk action `Route to specialist`, choose specialist `Inez Park`, set follow-up date `2026-07-29`, and add note `Temperature excursion verified; preserve chain-of-custody.`

The queue contains a stale selection from another region. Clear it before building the target selection. Changing filters must not lose correct target selections across pages. Open the bulk review drawer and verify it lists exactly those three IDs before applying once.

## Completion

All three target shipments must show `Specialist review`, Inez Park, the exact date and note. Every other shipment, including `NC-205`, `NC-220`, and the initially selected `SE-118`, must remain unchanged. Capture the decisive screenshot with the three-record review drawer visible before applying.
