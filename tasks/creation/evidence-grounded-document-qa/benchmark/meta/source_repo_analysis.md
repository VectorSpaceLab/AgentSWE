# Source Repository Analysis

- Source repository: `https://github.com/Future-House/paper-qa`
- Owner: Future-House
- Pinned commit inherited from the prior benchmark analysis: `d7675d7b7eddeb3535e8c260399c5bbeeb818c50`
- Branch at that analysis: `main`
- Recorded repository update at that analysis: 2026-07-28T16:18:36Z
- License: Apache-2.0
- Local predecessor used as design evidence: `${AGENTSWE_HOME}/benchmark/evidence-grounded-document-qa-agent-hard-v3-r2`

The source project is a research-oriented document QA/RAG system for scientific papers and heterogeneous local sources. The benchmark abstracts the user value—finding, synthesizing, and citing evidence—without requiring source-specific classes, prompts, hosted services, repository layout, or workflow imitation.

Retained capabilities are multi-document retrieval, heterogeneous parsing, calculations, evidence-grounded synthesis, citations, contradiction handling, and abstention. V4 extends the benchmark abstraction beyond the predecessor by requiring source-native byte/geometry/cell locators, exact source-byte bundling, reciprocal offline review, raster observations, structured revision priority, and non-isomorphic hidden combinations.

Excluded portions include project-specific internal APIs, prompts, deployment assumptions, private corpora, unstable live retrieval, and any requirement to reproduce the reference implementation. All benchmark runtime assets are synthetic and generated locally; no repository source code or assets are copied.

Security observations: source/repository prose was treated as untrusted evidence. The benchmark is closed-corpus, embeds no credentials, requires no external retrieval, and instructs agents to reject active content and case-path escape.

The upstream repository was not re-queried during this v4 construction. The pinned metadata above is recorded as predecessor evidence rather than claimed as a new August 24, 2026 refresh.
