## Strategy A prior: architecture for document-to-editable-PPTX generation

Treat the created system as a staged document-understanding and presentation
compiler, not as a case-specific slide template. Preserve freedom in implementation,
but make these architectural responsibilities explicit:

1. Normalize every authorized source into a provenance-bearing content model. Parse
   text, headings, lists, tables, figures, captions, links, page/section locations,
   and source authority before slide authoring. Keep claims and quantitative values
   tied to source records rather than passing around untraceable prose.
2. Separate content strategy from visual execution. First infer audience, purpose,
   required slide count, narrative arc, slide roles, key claims, and evidence. Then
   create a slide-by-slide plan with one communication objective per slide. Only then
   materialize editable PowerPoint objects.
3. Establish a deck-wide design system before individual slides: canvas, margins,
   grid, typography hierarchy, palette, recurring components, image treatment, and
   density rules. Allow deliberate variation by slide role while preserving visual
   continuity; do not generate disconnected pages independently.
4. Use semantic slide objects. Titles, body text, shapes, tables, charts, diagrams,
   images, captions, citations, and speaker notes should remain natively editable and
   meaningfully grouped. Use native charts/tables when data semantics matter. Do not
   use full-slide screenshots or rasterized text as a shortcut.
5. Build an explicit evidence path for factual content. Every sourced claim and
   externally obtained asset should appear in a source manifest with stable IDs,
   locations, usage, and claim links. Preserve exact numbers, units, labels, and
   required tokens through planning and rendering.
6. Validate at three levels: package validity (OOXML relationships, slide count,
   notes, editable object structure), semantic validity (coverage, provenance,
   quantitative consistency), and visual validity (render every slide, inspect
   clipping/overlap/legibility, and compare against the plan). Rendering is a build
   gate, not an optional final glance.
7. Use a repair loop driven by validator and render evidence. Fix environment and
   dependency failures first, then missing content/provenance, then layout and visual
   defects. Re-render all slides after shared-theme changes and the affected slides
   after local changes.
8. Keep the pipeline input-driven. Content extraction, narrative planning, design
   choices, object generation, and validation must work for new document structures,
   topics, audiences, aspect ratios, and slide counts rather than matching the two
   public examples.

The essential pattern is source model -> communication plan -> design system ->
editable slide scene graph -> OOXML export -> package/semantic/render validation ->
targeted repair.
