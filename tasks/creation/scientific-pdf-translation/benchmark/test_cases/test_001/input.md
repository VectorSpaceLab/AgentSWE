# Interactive Translation Request: Calibración de Flujo Criogénico

Translate [source.pdf](assets/source.pdf) from Spanish to English for a metrology audit team and create the complete interactive reading bundle.

## Translated PDF

Produce a **monolingual English PDF** with exactly three pages. Pages 1 and 3 are A4 portrait; page 2 is A4 landscape and is stored with page rotation. Preserve the numbered sections, six-column calibration table, tolerance-band plot, approval block, and the exact orientation sequence.

Translate all prose, headings, table headers, plot labels, captions, notes, and approval-role labels. Preserve instrument codes `FM-08`, `REF-2`, and `CAL-77`, technician names, dates, values, decimal commas, units, acceptance marks `✓` and `△`, and `E_r = (q_ind - q_ref) / q_ref × 100 %`. Translate “incertidumbre expandida” consistently as “expanded uncertainty”.

Page 2 contains protected sensor tokens `PT-100-A`, `Δp_ref`, and `QC_LOCK` inside visually dense shaded table/plot regions. They must remain exact and visible; translated labels must not cover values, error bars, legend marks, or those tokens.

## Alignment and Viewer

Represent the rotated page geometry correctly after displayed rotation. Provide usable alignments for landscape table row groups, the plot caption, and surrounding prose. Clicking a page-2 source overlay must scroll to the correct page-2 target region, and the reverse interaction must return to the source box rather than only matching page number.

Use the PDF as a closed corpus. Do not normalize decimal commas, explain the acceptance marks, or add audit conclusions.
