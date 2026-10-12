# Phase-A source audit

## Authoritative identity

- Source: `@@AGENTSWE_EDITING_SOURCES@@/20-edit-openwiki-change-impact`
- v2 manifest source digest: `87dea0aea5c019f4a37799587b1eed60e313e8dcca53a67f5feba11fd42229b3`
- Native pinned repository tree digest: `9f89b2c56a072f2e5c63d3b60bd6e7c7e9f56861ee1500e0925603ab938e1160`
- Upstream commit: `630eb9ec3fa22a4bed2d347fc3ea3a6a3bd22abc`

## Entrypoints and calls

The shipped package declares `bin.openwiki = ./dist/cli.js` and requires Node
22+. `src/commands.ts` parses `--init`, `--update`, `--print`, `--modelId`, and
the code mode. `src/cli.tsx` calls `runOpenWikiAgent()` for update runs.
`src/agent/index.ts` loads environment/configuration, resolves the provider and
model, creates the DeepAgents graph, and streams the run. `createModel()` uses
`ChatOpenAI` for OpenAI-compatible providers and can use the Responses API;
OpenWiki also supports several other providers, which the lower protocol must
not silently select.

The agent's operational surface is the `OpenWikiLocalShellBackend` plus
connector tools. In repository mode it applies docs-only writes, honors
`.openwikiignore`, and finalizes through OKF front matter/index synchronization,
Mermaid validation, and internal-link validation. Persistent update metadata is
written by `src/agent/utils.ts`; checkpoint state is backed by SQLite and the
active-run crash guard records interrupted runs.

## Native benchmark boundary

The old runner applies a patch to the pinned source and invokes the compiled
CLI. Its oracle checks impact reports, executable examples, immutable static
publication, full-text search, persistent transaction receipts, recovery,
concurrency, and tenant isolation. It is valuable mechanism coverage, but the
old Result is native CLI behavior and does not prove a real lower LLM rollout.

## Migration decision

The lower launcher therefore invokes the candidate's own compiled OpenWiki CLI
with a case request and evaluator-owned broker environment. The evaluator
captures model calls and tokens around the invocation and inspects both product
artifacts and the model-authored result contract. A generic Codex command is
not used as a substitute for OpenWiki.
