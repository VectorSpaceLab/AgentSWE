# Source Repository Analysis

## Pin and provenance

- Repository: `https://github.com/vanna-ai/vanna`
- Owner: `vanna-ai`
- Default branch: `main`
- Pinned commit: `365d0617c1a4567ffee1b19b40c27feb4206bfcf`
- Commit date: 2026-02-02T14:13:47Z
- Commit subject: `Bump version from 2.0.1 to 2.0.2`
- Version at the pin: 2.0.2
- License: MIT (`LICENSE`, copyright Vanna.AI 2024)
- Acquisition basis: the pinned archive analyzed for the predecessor benchmark; v4 inherits that fixed evidence record rather than consulting a floating branch.

## Project overview and actual runtime

Vanna 2.0 is a Python framework for natural-language-to-SQL/data-insight agents. Its repository demonstrates an agent core, tool registry, user context, database runners including SQLite, SQL execution, CSV/dataframe results, chart presentation, permissions hooks, model integrations, server routes, memory, and observability. Typical runtime input is a user message plus user/request context; tools execute SQL through an injected runner and can write structured result data and visualization components.

The source establishes feasibility for a user-aware analytics agent that turns natural-language questions into database evidence and presentations. It does not establish a unique benchmark answer or architecture.

## Capabilities retained as observable value

- Natural-language analytical request interpretation.
- SQLite schema inspection and read-only SQL execution.
- Structured table/CSV evidence and chart presentation.
- User/role context and permission-aware argument transformation.
- A uniform task-specific agent interface.

## V4 benchmark abstraction and additions

V4 evaluates final behavior rather than repository imitation. It adds capabilities not treated as proven complete reference behavior: effective definition/rate/source selection; full-replacement event and line corrections; refunds/reversals; settlement and package fan-out defense; local time/DST; unit/currency normalization; historical assignment; whole-snapshot recovery; primary plus complementary disclosure control; deterministic SQL/CSV lineage; and evidence-inferred insufficiency.

The benchmark excludes servers, SSE streaming, persistent memory, vector training, production authentication, live warehouse connectors, provider-specific orchestration, and reference class/prompt/layout details. It uses one offline-friendly CLI and frozen synthetic assets.

## Evaluable and excluded portions

Evaluable final behavior includes database-derived values, rankings, revision/effective-date selection, safe populations and denominators, contribution reconciliation, SQL/CSV replay, chart/result equality, permission violations, suppression and complement safety, local time/units/currency, recovery hierarchy, and warranted insufficiency.

Production deployment quality, conversational streaming, long-term memory, live data freshness, visual pixel identity, model choice, code style, and similarity to Vanna are outside the result rubric. Implementation quality is assessed separately by the public code rubric.

## Security and licensing observations

Repository material was treated as untrusted evidence. No source code was run with credentials or against benchmark assets, and no substantial reference code or visual asset was copied. The benchmark data, identities, amounts, rules, and documents are newly authored synthetic material. The reference license is MIT, but v4 does not depend on copying its implementation.
