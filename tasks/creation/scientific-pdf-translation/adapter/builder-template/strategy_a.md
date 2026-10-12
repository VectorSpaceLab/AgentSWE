## Strategy A prior: architecture for scientific PDF translation

Treat the created system as a geometry-preserving document translation pipeline, not
as page-level text replacement. Preserve freedom in libraries and models, but make
these architectural responsibilities explicit:

1. Analyze each page before translation. Render and parse it, identify native versus
   scanned content, recover reading order and layout regions, and classify prose,
   formulas, code, tables, figures, captions, footnotes, references, headers, and
   protected identifiers. Use OCR or visual recovery only where native extraction is
   inadequate, with confidence attached to recovered units.
2. Maintain an intermediate document model whose units carry source page, reading
   order, text, geometry, type, protection policy, and confidence. Translation should
   operate on coherent prose units while scientific objects and their associations
   remain explicitly protected.
3. Separate extraction, translation, and reconstruction. Batch or cache translation
   where useful, enforce glossary/protected-token constraints, and validate numbers,
   signs, units, citations, equations, identifiers, negation, and modality before
   placing target text back into the document.
4. Reconstruct adaptively within the original page geometry. Choose suitable fonts,
   script direction, line breaking, local reflow, and bilingual pairing. Preserve
   page count, dimensions, rotations, columns, object associations, and page-local
   reading order while avoiding clipping, overlap, and text painted across graphics.
5. Derive alignment from the same intermediate geometry used for reconstruction.
   Map meaningful source/target units with real page boxes, multiple anchors when a
   unit splits, stable reading-order IDs, digests, status, and calibrated confidence.
   Do not invent decorative boxes or align against a detached transcript.
6. Build the viewer as an offline projection of the PDF and alignment artifacts.
   Bundle the renderer locally; use the alignment geometry for overlays; support
   bidirectional pointer and keyboard activation, active/ARIA synchronization,
   counterpart scrolling, page identity, and visible uncertainty.
7. Validate in layers before success: parse and render every PDF page; compare page
   count/dimensions/rotation; check glyph coverage and protected-token survival;
   validate alignment schema, bounds, order, digests, and visible overlap; then run a
   clean-browser offline interaction smoke at both required viewports.
8. Recover honestly. Retry bounded OCR/translation/layout operations, isolate
   page-local failures, preserve uncertain visible tokens, lower confidence when
   warranted, and never silently omit a page or fabricate an alignment.

The reusable pattern is page analysis -> typed geometry model -> constrained
translation -> adaptive reconstruction -> geometry-derived alignment -> offline
viewer -> parse/render/browser validation and targeted repair.
