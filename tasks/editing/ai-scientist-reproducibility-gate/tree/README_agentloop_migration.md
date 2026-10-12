# AI Scientist reproducibility-gate Agent-loop sibling

This sibling is an additive, auditable migration layer copied from the
authoritative source. The source, old staging and formal runs remain read-only.
The original native public/hidden harness is retained for provenance and
mechanism comparison; Agent-loop Result uses `agentloop/` and measures the
edited AI Scientist lower Agent.

Run static checks:

```bash
python3 agentloop/self_test.py
python3 -m compileall -q agentloop
python3 agentloop/code_score_runner.py --candidate <delivery> --result <out.json>
```

`self_test.py` is a protocol smoke only. It does not call a provider and cannot
prove that a real lower Agent made a successful broker call. A Stage-B simple
pilot must start the evaluator-owned `agentloop/broker.py`, use a real/buildable
candidate, run at least one public case, inspect broker stats for
`successful_calls > 0`, then run one hidden smoke only after the latest
accepted Candidate is explicitly frozen. Any such pilot remains labeled smoke,
not a paper/formal Result.

The lower launcher invokes the candidate's own `ai_scientist.claim_verification`
module when available and falls back only to the edited product launcher with
`--verify-claims-only`; it never invokes generic Codex to solve the case.

Current Stage-A state is `PARTIAL`: the migration and static gates exist, but no
real successful broker call has been made in this audit turn, and no formal
Builder up-to-ten-round or Code evaluation was started.
