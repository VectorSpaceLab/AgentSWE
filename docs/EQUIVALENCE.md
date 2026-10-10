# Released evaluator vs. the evaluator used for published results

For the Creation tasks checked below, the released evaluator differs from the one that produced the
published numbers in ways that do not change scores: host paths and internal service names are
configuration, provider endpoints go through per-role brokers, and the gateway provider-count keys have
neutral names (candidates and judge outputs written with the old names are read through a compatibility
layer). The original files contain internal host names and are not published; `provenance/paper-exact/`
lists their sha256 digests so an archived copy can be authenticated on request. The equivalence below
covers those Creation tasks only; the Editing and Optimization releases carry adaptations that can change
scores, and the task-level ones are described in each task's `task.json` (`notes`).

Score equivalence is checked by replay, never by re-judging (judges are stochastic):

```bash
python3 tools/verify_equivalence.py creation --runs <archived runs dir> --report report.json
```

For every archived Result-judge case the archived judge output, trusted harness result and eval
manifest are fed to the released `verify_score.py`; the resulting score contract must equal the
archived one on score, validity and publishability. No model is called.

| Family | Archive | Cases | Identical |
|---|---|---|---|
| Creation (the paper's Lite runs, DeepSeek-V4.1-Flash builder: Repository bug repair, Database analytics, Scientific PDF translation; 11 runs incl. reruns) | 2026-10-01 | 66 | 66 |

Deliberate evaluator fixes are listed in each task's `task.json` (`notes`) and reported by the
tool as `expected` differences when an archived case is affected.
