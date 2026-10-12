# Task Goal

## Background

High-stakes research questions are rarely answered by the first search results. Names collide, policies change, product pages are superseded, papers qualify headline claims, websites repeat one another, and public tables use incompatible units. A useful researcher must follow evidence beyond snippets, resolve identities and dates, inspect page bodies and PDFs, test whether apparent corroboration is independent, expose contradictions, and make calculations reproducible.

## Agent to Create

You must create a **Deep Evidence Research Agent**. It receives one Markdown research request and produces a decision-useful report plus machine-inspectable source and evidence records. All benchmark cases permit and require live public-web research through the declared Search/Scrape layer.

## Target Users

The users are research leads, policy and compliance teams, technical evaluators, museum and public-history staff, public-data analysts, and procurement teams who need a conclusion they can audit rather than a collection of search results.

## Core Value

Your agent must:

- Turn an ambiguous or multi-part request into a focused research plan and refine queries as identities, terminology, dates, and evidence gaps become clearer.
- Search beyond the first result page when useful, follow relevant links, and inspect the actual bodies of selected HTML pages, repositories, release notes, datasets, and PDFs.
- Distinguish entities with similar names, current rules from superseded ones, publication dates from effective dates, primary evidence from later retellings, and independent corroboration from copied or shared-source claims.
- Verify consequential claims across appropriate source types and represent both support and material contradiction.
- Extract and normalize public data, show formulas and inputs, recompute results, and state meaningful uncertainty or sensitivity bounds.
- Recover honestly when a preferred page is blocked, incomplete, or dynamically rendered by using an isolated local browser or another authoritative route.
- Produce a concise decision report whose citations resolve to exact, inspectable passages in a structured evidence graph.

## Final Objective

For every valid request, generate four complete UTF-8 artifacts in the requested output directory:

- `report.md`
- `sources.json`
- `evidence_graph.json`
- `run_report.json`

The same implementation must generalize across the public development cases and six unseen cases. Search snippets may guide discovery, but a snippet alone is categorically insufficient support for a material report claim.

## Non-Goals

You are not required to:

- Reproduce any particular repository, framework, prompt, agent hierarchy, or tool sequence.
- Expose private chain-of-thought, hidden deliberation, or a complete browsing transcript.
- Produce a slide deck, web application, academic manuscript, or fixed reference answer.
- Maximize source count without regard to relevance, independence, or access depth.
- Bypass access controls, authenticate to public sites, submit forms, or use sources that are not legally and publicly accessible.
- Force certainty when the best-supported conclusion is conditional or unresolved.
