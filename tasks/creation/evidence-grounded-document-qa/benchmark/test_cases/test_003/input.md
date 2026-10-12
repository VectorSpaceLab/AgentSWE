# Request: Orchid assay result reconstruction

Reconstruct the reportable results for Orchid-K and Control-Q from `assay_method.docx`, `plate_reader.csv`, `standard_curve.svg`, and `revision_notice.pdf` in `assets/`.

Apply the approved lot-specific blank and any well invalidation before calculating. For Orchid-K, calculate each valid replicate concentration with the source formula and dilution factor, then report the replicate mean. For Control-Q, follow the method's instruction for invalid wells, calculate the reportable concentration from remaining valid evidence, and determine whether the quality-control range is met. Use the curve slope and units shown in the SVG; do not estimate a different slope from pixel positions.

Explain the conflict between the draft and approved blank and the status of invalidated well B07. State whether 30-day sample stability is known. Show arithmetic to sensible precision and distinguish raw absorbance, blank-corrected absorbance, undiluted concentration, and dilution-adjusted concentration.

Deliver a concise result table and conclusion in `answer.md`. `claims_and_citations.json` must contain atomic claims for formula terms, well validity, each calculation stage, QC status, revision priority, and abstention. Use DOCX paragraph/table-cell locators, CSV row/column byte ranges, SVG element IDs plus normalized geometry, and PDF rectangles overlapping the relevant extracted text. Link all IDs and locator objects exactly into `review_manifest.json`.

The offline viewer must preserve and expose every cited original source and replay reciprocal selection across all four modalities. Separate supporting evidence for the approved blank from contradicting evidence for the superseded draft value. Closed corpus; do not search.
