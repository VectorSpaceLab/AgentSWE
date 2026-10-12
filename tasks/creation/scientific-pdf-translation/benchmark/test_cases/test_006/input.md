# Interactive Translation Request: Confocal Membrane Transport Atlas

Translate [source.pdf](assets/source.pdf) from English to Traditional Chinese for cell-imaging researchers. Apply [glossary.csv](assets/glossary.csv) as authoritative and create the complete interactive reading bundle.

## Translated PDF

Produce a **monolingual Traditional Chinese PDF** with the same three portrait pages. Preserve the two-column methods text, four-panel confocal figure, spectral heatmap, compact results table, callout arrows, scale bars, captions, footnote, and references. Use Traditional Chinese glyph forms throughout ordinary translated prose.

The glossary contains a deprecated and a preferred target for “membrane flux”. Use only `preferred_target`; the deprecated value must not appear. Preserve *Arabidopsis thaliana*, `MTR-7`, `ROI_A3`, `Na+/H+`, `λ_ex=488 nm`, `F_corr = F_raw - B_λ`, panels `(A)`-`(D)`, `2 µm`, `500 nm`, values, units, and citations.

The protected tokens `ROI_A3`, `Na+/H+`, and `λ_ex=488 nm` occur inside visually dense figure/heatmap regions. They must remain legible and attached to the right panel; translated callouts must not cover image data, scale bars, contours, or arrows.

## Alignment and Viewer

Align prose, captions, table row groups, footnote, and reference entries. For figure callouts, use boxes that correspond to their visible label regions rather than one rectangle over the whole figure. Bidirectional selection of the Figure 2 caption and at least one translated callout must reveal the matching rendered region on the other side.

Use only the supplied assets. Do not infer biological mechanisms or replace scientific panels with decorative imagery.
