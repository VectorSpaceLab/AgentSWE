# OpenWiki Agent-loop migration plan — owner-20

## Scope and read/write boundary

This sibling is an additive migration of the authoritative OpenWiki edit
benchmark. The authoritative source, `@@AGENTSWE_LEGACY_HARBOR@@/0830-edit-v2`, old
formal runs, shared Harbor framework, and main repair manifest are read-only.
Only this sibling is a write target. The copied native harness remains useful as
a mechanism/build comparison, but it is not the Agent-loop Result.

## Real lower product and model surface

The lower agent is the candidate's patched OpenWiki CLI, not a generic coding
agent. The production entry is `dist/cli.js`, reached through the package bin
`openwiki` and dispatched by `src/cli.tsx`. The lower command is:

```text
node dist/cli.js --update --print --modelId gpt-5.6-sol <case request>
```

The execution path is `commands.ts` argument parsing → `cli.tsx` run dispatch →
`runOpenWikiAgent()` → `createOpenWikiAgent()`/`createDeepAgent()` → the edited
`OpenWikiLocalShellBackend`, connector tools, and OKF/link/Mermaid middleware.
The model construction seam is `src/agent/index.ts:createModel()`, where the
evaluator-owned OpenAI-compatible Responses endpoint is injected through the
candidate environment. The broker overwrites model and reasoning effort, so a
candidate cannot select a different lower model.

## Product task contract

Each case asks the OpenWiki agent to inspect a repository change, update only
the affected documentation, preserve hand-written material, execute marked
examples, and emit a machine-readable impact report. The report must bind the
document diff, transaction receipt, publication manifest, and search index to
the same case/request/generation identity. A no-op, duplicate, stale-owner
recovery, or conflict must be reported honestly rather than represented as a
fresh documentation edit.

The existing native case fixtures already exercise rename, type/default/API
migration, partial staging, concurrent duplicate, and security/no-op behavior.
The Agent-loop layer adds a real model-authored trajectory and final artifact
contract around those product actions.

## Case, tool, state, and artifact map

| Surface | What the lower agent sees/uses | Evaluator-owned evidence |
|---|---|---|
| Request | case-local task text, a nonce, repository fixture, allowed output schema | expected affected pages, stale kinds, exact dynamic values |
| Tools | OpenWiki filesystem tools, git/read-only discovery, edited CLI commands, case-local helper only when the candidate implements the protocol | invocation sequence, arguments, exit status, resource bounds |
| State | repository `openwiki/`, `.openwiki-impact/` tenant/receipt state, publication/search roots | preseeded stale/partial/corrupt state, tenant and generation identity |
| Artifacts | Markdown pages, `impact-report.json`, `documentation.diff`, receipt, publication and search manifests, `agent_result.json` | byte hashes, active pointers, cross-surface equality, hidden oracle |

The evaluator must distinguish raw CLI/tool events from model-authored text.
`agent_result.json` is the lower-agent result contract; if it is absent, the
case is a candidate contract failure, not an evaluator success.

## Dynamic oracle and anti-leak design

`agentloop/evaluator/dynamic_case_service.py` creates a fresh nonce and stores
the expected dynamic facts only under the evaluator run directory. It gives the
candidate a case request and nonce, but strips expected pages, expected example
stdout, forbidden terms, receipt bytes, and oracle values. The lower container
mounts only the patched product, a fresh repository/workspace, the case client,
and the placeholder credential. It does not mount `test_cases/`, evaluator
source, hidden manifests, historical runs, or the real credential.

## Lifecycle

The controller is deliberately finite and single-session. It accepts up to ten
distinct Candidate snapshots, runs both public cases after every acceptance,
returns fresh feedback to the same Builder session, and freezes the latest
accepted snapshot when the Builder exits or the limit is reached. A `dev_passed`
flag is feedback only and does not trigger automatic freeze:

```text
accepted Candidate 1..10 → dev_001 + dev_002 after each acceptance
                        → fresh authoritative feedback
                        → freeze latest accepted Candidate → hidden only after freeze
```

Infrastructure-invalid executions do not consume a candidate round. Duplicate
digests are idempotent. Hidden execution is rejected before freeze.

## Build/materialize adapter

`agentloop/candidate_adapter.py` validates the exact three-file delivery,
applies the repository-relative patch once to the pinned source, records the
candidate digest, and exposes a build command for the fixed Node/TypeScript
product. The adapter never copies evaluator files into the candidate. Build
failure is candidate-owned; broker, provider, credential, mount, or evaluator
failure is infrastructure-invalid.

## Result and Code separation

Agent-loop Result scores the actual lower OpenWiki run: successful broker calls,
production CLI/tool trajectory, dynamic impact facts, receipt/provenance,
publication/search artifacts, honest recovery/idempotence, and safety/privacy.
The independent Code axis scores the frozen patch using the required eight
dimensions and never reads Result files. Result and Code are not summed or
averaged.

## Phase-A cost and risks

Static work is cheap. A future simple pilot needs one buildable candidate, two
dev lower-agent calls (one case each), one freeze verification, and one hidden
smoke. Expected order of magnitude is 2–8 successful lower calls per case and
roughly 20k–120k broker tokens for a simple pilot, subject to OpenWiki tool
loops; full Builder/formal runs are materially larger and are out of scope.

Main risks are OpenWiki's provider configuration rejecting the synthetic model
ID, LangChain choosing a non-Responses transport, SQLite/checkpoint state
escaping case isolation, a candidate writing the report outside the allowed
wiki/output roots, and the product's normal interactive UI obscuring a stable
noninteractive result. These must be tested in a future pilot, not guessed from
static self-tests.
