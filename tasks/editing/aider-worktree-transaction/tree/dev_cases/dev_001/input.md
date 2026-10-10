# Public case: quarantined superproject/submodule publication

Use `assets/manifest.json` through the public schema-v3 adapter. The runner
creates a root repository with a registered local `vendor/core` submodule.
Independent workers edit the root and the component in separate linked
worktrees. The component candidate is composed first; the root candidate
binds its `160000 vendor/core` entry to that exact component commit.

Exit cooperatively after the durable federation prepare. At that boundary all
four root/component target refs remain at their declared bases, and neither
candidate is readable from the corresponding ordinary object database, while
both are readable from the verified transaction quarantine. Recovery under
the same owner records the commit decision, promotes each closure, and
publishes the component before the root. The runner verifies every ref, the
candidate parent/tree/gitlink, the supplied checkouts, command counts,
cleanup/admission release, and stable status.

This case demonstrates the complete success path and the authority rules.
Run `run.sh` after installing the candidate; do not special-case public
paths, IDs, repository counts, content, or OIDs.

The evaluator uses the same set of independent observations for public and
hidden cases: real refs, the root tree's `160000` gitlink entries, each
participant's `aider/transactions/` admission bytes, the `state_dir/ledger.json`
bytes, `git worktree list`, and the append-only execution record of the
evaluator-owned worker/test commands. Publication success, exactly-once,
read-only `status`, transaction ownership release, and pre-decision quarantine
are all judged from those bytes. See the "Verifiability" section of
`input/02_interface_and_delivery.md` for the checklist.
