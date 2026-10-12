# Hidden case 4: incomplete evidence and partial staging reconciliation

Maintain the service after a runtime edge changed from Cache to Store,
a setup step moved to bootstrap, and a command's output changed. The
processed coordinated request starts with an expired prepared claim,
truncated staged documentation files, and no valid committed receipt,
indicating a process failure between prepare and install.

Do not publish success from that evidence. Recompute and verify the
complete change, then repair the Mermaid edge, the related file/anchor
links, and the exact example output once. Skip the incomplete pseudocode,
contain the example sentinel, preserve the operations text and the
deployment page, and leave a valid receipt whose installed hashes agree
with every changed page. Remove all incomplete staging and lock state.

Reconcile the partial publication release and search generation as
untrusted bytes. Install one complete static site and index whose
generation, digest, page hashes, and search results agree with the
recovered receipt; readers must never observe the truncated generation.

Product contract (identical for every case): do all work through the
compiled production CLI; do not hand-edit documentation or forge state
files. The impact report must list only the genuinely affected pages and
give each a `direct` flag and non-empty `reasons`; `changed_paths` must
equal the set of documents that actually changed. The new index generation
must no longer return text removed by this change, while every query named
by the case must be answerable by the page that should answer it. Write
`agent_result.json` as soon as the first product response arrives and
refresh it after every step, emitting the same object verbatim as the last
JSON value on stdout; when stopping early, use `partial` to honestly list
the parts not attempted and never claim steps that were not executed.
