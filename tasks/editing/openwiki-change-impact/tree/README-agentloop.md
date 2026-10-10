# OpenWiki change-impact Agent-loop sibling

This directory is the Phase-A additive sibling for `OWNER=owner-20`. It keeps
the authoritative native benchmark content for provenance while adding a real
lower-agent protocol for the patched OpenWiki CLI.

Static checks are intentionally provider-free:

```bash
python3 agentloop/self_test.py
python3 -m compileall -q agentloop dev_cases evaluator/harness evaluator/tests
python3 -m py_compile agentloop/*.py agentloop/evaluator/*.py
```

The lower launcher is `agentloop/evaluator/lower_agent_launcher.py` and the
evaluator-owned broker/lifecycle is `agentloop/evaluator/broker.py`. The
broker forces `gpt-5.6-sol` with reasoning effort `medium`, while the Candidate
receives only `broker-only-placeholder`. Calls, failures, prompt tokens,
completion tokens, and total tokens are recorded.

The lifecycle is one continuous Builder session with up to ten distinct
accepted submissions. Every accepted submission runs both dev cases and
receives fresh evaluator feedback. A duplicate digest is idempotent and does
not consume a round; Builder exit or the ten-round limit freezes the latest
accepted Candidate. A passing dev mean is recorded feedback, not an automatic
freeze. Only after that freeze does the one-time hidden gate dispatch the six
canonical hidden cases.
`agentloop/evaluator/controller.py` and
`agentloop/evaluator/hidden_controller.py` enforce the order. The freeze records
both Candidate and `repository_digest` values, seals the manifest, and removes
write bits from the frozen tree. Each hidden case records before/after frozen
and disposable-workspace digests plus an evidence manifest. The Result rubric
and independent eight-axis Code rubric are separate.

Phase-A status remains `PARTIAL` for formal scoring: this sibling now has a
provider-free verified freeze/hidden control path and safe local broker smoke,
but no expensive formal Builder run or formal Result/Code score is claimed.
The implementation and verification record is
`meta/openwiki_agentloop_freeze_hidden_repair_20260902.md`.
