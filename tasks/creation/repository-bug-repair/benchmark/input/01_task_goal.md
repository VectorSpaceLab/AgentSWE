# Task Goal

Build a reusable **Repository Bug Repair Agent** for maintainers who provide a behavior-level issue report and a complete local repository snapshot.

Your agent must inspect unfamiliar multi-module code, establish the failure with executable evidence, diagnose interactions across APIs, state, caches, persistence, serialization, concurrency, and protocol boundaries, implement a compatible repair, and return a patch that applies to a pristine checkout. Public tests can all pass before the repair; the issue report and repository are evidence to investigate, not an answer key.

The final objective of every successful run is a portable `solution.patch`, truthful `repair_report.json`, and `run_report.json`. Cases that explicitly require migration or recovery also require `migration_report.json` and an executable repository artifact whose self-test exercises the real persisted format.

This is not a review, triage, or advice task. Do not return only prose, edit supplied assets in place, copy out a modified repository, weaken tests or build files, add forbidden dependencies, hard-code known cases, fabricate command evidence, or rely on a human to finish the repair.

