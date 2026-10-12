# Public case: partial-decision recovery and repository-scoped admission

Use `assets/manifest.json` through the public schema-v3 adapter. The primary
plan spans the root and a local component repository. Kill its process group
after the durable commit decision and the component participant's ref commit,
but before the root is promoted or published.

While the old lease is still valid, status must immediately report the
committed prefix without mutation. A create that uses a different state root
but an overlapping component repository is refused by repository-scoped
admission, while a third plan on a disjoint local repository is admitted and
commits normally. After expiry, takeover at the next fence must roll the
original decision forward: promote and publish the unchanged root candidate
whose gitlink names the already-committed component candidate.

The runner verifies the temporary partial prefix, non-blocking status,
overlapping/disjoint admission behavior, decision and candidate identity,
final refs/gitlink/checkouts, exactly-once commands, stale-fence rejection,
admission cleanup, and stable status. Recovery must not compensate the
component or rebuild either candidate.

The evaluator uses the same set of independent observations for public and
hidden cases: real refs, the root tree's `160000` gitlink entries, each
participant's `aider/transactions/` admission bytes, the `state_dir/ledger.json`
bytes, `git worktree list`, and the append-only execution record of the
evaluator-owned worker/test commands. Publication success, exactly-once,
read-only `status`, transaction ownership release, and pre-decision quarantine
are all judged from those bytes. See the "Verifiability" section of
`input/02_interface_and_delivery.md` for the checklist.
