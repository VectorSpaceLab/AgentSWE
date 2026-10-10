# Task goal

You are extending the existing OpenWiki repository, not creating a separate
tool. OpenWiki is a TypeScript CLI that maintains repository documentation
under `openwiki/` and already ships a live wiki visualizer. Its production
update path is `openwiki --update`.

Build one coherent offline maintenance workflow with four observable
capabilities:

1. Repair the minimal set of source-derived generated documentation for a
   public code change and safely execute complete documentation examples.
2. Make coordinated requests durable across process failures, lost
   responses, retries, stale generations, and overlapping workers.
3. Export a self-contained static wiki site that can be copied to ordinary
   static hosting and opened without running the OpenWiki server or a CDN.
4. Build a durable full-text index and expose deterministic search through
   the compiled production CLI.

Maintainers need reviewable documentation diffs. Operators need proof that a
logical request committed once. Readers need a browsable snapshot and search
results that describe the same installed documentation generation. A failed
or partial publisher must never leave the maintained documentation, the
active site, the active index, and the durable receipt inconsistent.

The target users are API-library and monorepo maintainers who invoke
OpenWiki manually, in CI, or through replicated workers, and then publish
the generated wiki to operators and developers.

The end goal is one deterministic production-integrated path that
generalizes across API renames, moves, changes, deprecations, and removals;
publishes useful offline reader artifacts; removes stale search claims; and
passes safely through restart, duplicate, conflict, corruption, and
concurrent delivery scenarios.

## Non-goals

- Do not build a standalone analyzer, a report-only sidecar, test hooks, or
  a new daemon.
- Do not replace init, chat, provider, connector, translation, or the
  existing live `visualize` command.
- Do not regenerate the whole wiki when a bounded affected set is known.
- Do not execute pseudocode, source comments, prose, expected output, or
  remote commands.
- The manifest-driven path (`openwiki --update --impact-manifest ... --print`)
  needs no model, external search engine, database service, network host,
  or distributed lock service at product runtime. Case execution under the
  agent loop is the exception: it must be completed through model calls to
  the evaluator's broker; see "Agent-loop result contract" in
  `02_interface_and_delivery.md`.
- Do not force static publication or search for ordinary impact manifests.
- Do not hard-code development identities, paths, query strings, symbols,
  prose, participant counts, or outputs.
