# Task goal: crash-recoverable repository-set transactions

Extend the supplied Aider repository with a **Crash-Recoverable Repository-Set
Transaction Coordinator**. Aider must run a bounded DAG of isolated local Git
worktrees across a declared superproject and its local component
repositories, compose one candidate commit per repository, bind the
superproject gitlinks to the exact candidate components, and publish the
repository set under one durable roll-forward decision. This is an Edit
benchmark inside Aider, not a separate Git service.

The target users are developers and local automation making one logical
change across a superproject and its submodules while other tools may
independently run transactions, fetch, commit, inspect status, garbage
collect, or move refs. The core value is explicit authority at every
production boundary: repository-scoped admission prevents overlapping writers
without serializing disjoint repositories; prepared candidates exist only in
transaction-owned Git quarantine; and no public ref changes before one
synced global decision binds all participants, candidates, gitlinks, expected
ref OIDs, object closures, and the current epoch fence.

Git cannot commit refs in different repositories atomically. The observable
contract therefore uses a recoverable atomicity model. Before the durable
decision, any conflict, drift, crash, or corruption aborts all publication.
After that decision, recovery may only roll the same prepared candidates
forward in the declared child-before-parent order. A successful response is
impossible until every participant is published and the supplied worktrees
are synced. A crash may expose a temporary partial prefix; status reports it
read-only, and recovery completes it without recomputation. If a foreign
writer obstructs an uncommitted participant after the decision, recovery
fails closed with stable evidence and never overwrites the writer or rolls
back already-decided refs. The transaction remains recoverable after
worktrees, task refs, reflogs, alternates, loose repository objects, or
registered submodule checkouts are deleted. Fidelity covers dirty index
stages, tracked and untracked bytes, executable modes, symlinks, filters,
hooks, rename/delete shapes, and `160000` gitlink OIDs. A higher durable
repository fence permanently disables every stale process's command, object
promotion, ref, checkout, cleanup, or rollback effect. Read-only status must
stay non-blocking even while another coordinator holds an admission lease or
Git holds a ref lock.

Do not implement LLM task planning, remote workers, hosting integrations,
automatic rebases, semantic conflict resolution, or distributed consensus.
Preserve ordinary `python -m aider --version`, normal Aider usage, Git hooks
and filters, submodule/worktree behavior, and repositories in which the
coordinator is not used.
