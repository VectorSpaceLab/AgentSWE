# Interactive Translation Request: Scanned Algal Growth Bulletin

Translate [source_scan.pdf](assets/source_scan.pdf) from English to German for a freshwater monitoring group and create the complete interactive reading bundle.

## Source Condition

All four portrait pages are image-only synthetic scans. They contain skew, speckle, a faint gutter, low-contrast type, one partially abraded line, a compact table, a line plot, and a handwritten-looking margin mark `CHECK B4`. Two adjacent methods paragraphs use very similar wording. `CHECK B4` is not prose and must remain visible but untranslated.

## Translated PDF

Produce a **monolingual German PDF** with the same four portrait pages and recognizable layout. Make recovered German prose searchable/selectable. Preserve sample codes `B1`-`B4`, *Chlorella vulgaris*, values, units, day labels, citation markers, and `μ = (ln N₂ - ln N₁)/(t₂ - t₁)`. Translate headings, printed paragraphs, table headers, plot labels, captions, footnotes, and references. Use consistent German terms for “growth rate”, “blank control”, and “optical density”.

## Honest Degradation and Alignment

Do not guess the abraded characters or force ambiguous similar paragraphs into high-confidence matches. Preserve the visible source token or mark uncertainty unobtrusively in the translated page. Use `low_confidence` or `unresolved` alignment status where warranted, show that state in the viewer, and keep reliable paragraphs fully interactive. A smaller honest alignment set is preferable to fabricated boxes or digests.

Do not use external research, remove the margin mark, or add a scan-quality disclaimer page.
