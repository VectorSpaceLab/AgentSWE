# Interactive Translation Request: Coupled Soil-Carbon Note

Translate two English PDFs into Korean for an environmental-modeling team and create one complete interactive reading bundle:

1. [part_alpha.pdf](assets/part_alpha.pdf)
2. [part_beta.pdf](assets/part_beta.pdf)

Apply [glossary.csv](assets/glossary.csv) as authoritative across both documents.

## Translated PDF

Produce one **monolingual Korean PDF** containing all three portrait pages of Part Alpha followed by both portrait pages of Part Beta, five pages total. Insert no cover or separator. Translate all prose, headings, table headers, captions, footnotes, appendix notes, and reference titles. Preserve `SoilLoop-2`, scenarios `S0`, `S1`, `S2`, dataset `SLC-26-09`, formulas, variables, citations, values, units, footnote markers, and reference ordering.

Follow the glossary exactly across both files. Keep the Part Alpha table and Part Beta sensitivity figure with their captions; do not merge similarly worded sections or deduplicate references.

## Alignment and Viewer

The viewer's `source.pdf` must concatenate the two supplied PDFs in the listed order. Global alignment page numbers must run 1 through 5, while each source anchor must retain the correct `document_id`. The `source_documents` entries must report starts 1 and 4. Bidirectional interaction must work across the document boundary, including a Part Alpha page-3 reference and the first Part Beta page-4 paragraph.

Both PDFs and the glossary form a closed corpus. Make zero search or retrieval calls and add no synthesis.
