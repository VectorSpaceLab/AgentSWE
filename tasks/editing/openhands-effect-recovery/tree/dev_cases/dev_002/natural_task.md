# Recover a browser handoff without leaking or duplicating work

An old browser owner can still finish delayed callbacks while a replacement tab
resumes this conversation, an observer tab needs bounded visibility, and a
remote workspace commit response was lost after the remote side persisted it.

Four outcomes are required, and they are scored independently. Partial work is
reported as partial work, and hidden cases are scored the same way.

1. **OHR911 — Foreign authority is denied.** The same grant token presented by
   the wrong tab, by a foreign backend, or against a neighbouring conversation
   must return a denied inspection with no checkpoint and no status summary.
2. **OHR912 — The replaced owner is fenced.** Re-establish ownership under a
   fresh lease. The previous generation's delayed callback must then be
   suppressed and must leave the durable event cursor exactly where it was, even
   though it carries a much larger cursor.
3. **OHR913 — Reconcile the uncertain commit rather than repeat it.** Confirm the
   already-committed target through the reconcile-commit path and apply it
   locally exactly once; do not issue a second remote commit.
4. **OHR914 — Nothing private crosses the boundary.** No granted inspection may
   carry a fencing token, a claim token or a grant token, and no public
   projection may expose paths, digests, tab or incarnation identities.

Restore the production event, sync and workspace paths through the modified
OpenHands services. History and socket echoes can use different UI wrapper ids; a
late response from a frozen browser must not produce duplicate ledger, ordinary
event or terminal output. Choose the actions from actual state and report what
remains blocked; do not invent successful recovery.
