# Task Goal

## Background

Professional presentations are often assembled from filings, public data, papers, web pages, spreadsheets, briefs, and visual evidence. The difficult work is not transferring prose into slides. It is finding and qualifying evidence, reconciling sources, recomputing decision-relevant numbers, choosing an honest visual form, and producing a deck whose content can still be audited and edited.

## Agent to Create

You must create an **Evidence-to-Editable-Presentation Agent**. It receives one Markdown request plus optional local assets, performs the research and analysis authorized by that request, and produces an evidence-grounded PowerPoint briefing.

## Target Users

The target users are analysts, researchers, policy teams, educators, product and operations leaders, communications teams, and boards that need a concise presentation they can inspect, present, and continue editing in ordinary PowerPoint-compatible software.

## Core Value

Your agent must turn incomplete or heterogeneous evidence into a useful visual argument. It must:

- Understand the audience, decision, evidence boundary, language, brand, layout, and delivery constraints.
- Read local evidence and, when authorized, discover and inspect relevant public sources beyond search snippets.
- Distinguish facts, calculations, interpretations, conflicts, assumptions, and unknowns.
- Recompute material analytical measures from source values and preserve an auditable formula trail.
- Create substantive visual storytelling with data graphics, comparison structures, timelines, processes, or explanatory diagrams where they improve understanding.
- Keep text, charts, tables, and diagrams natively editable rather than rasterizing slide content.
- Cite claims on slides and in a machine-readable source manifest.
- Produce accessible, presentation-readable slides that render reliably.

## Final Objective

For every valid request, generate a technically valid and editable `deck.pptx`, an auditable `source_manifest.json`, and a truthful `run_report.json`. The same implementation must generalize across public development cases and unseen cases without case-specific code.

## Non-Goals

You are not required to:

- Reproduce any particular repository, prompt, framework, template, or internal workflow.
- Produce a research memo, PDF, webpage, or set of slide images instead of the PowerPoint deck.
- Modify an existing presentation in place, create a reusable template product, or preserve an unknown third-party slide master.
- Add animation, audio, video, or transitions unless a case explicitly requests them.
- Treat live-web sources as authoritative when a case defines a closed corpus or a source-priority rule.
- Invent facts, translations, quotations, forecasts, calculations, or visuals to conceal missing evidence.
- Expose private reasoning or make implementation details part of the deliverable.
- Call a complete presentation-generation service.
