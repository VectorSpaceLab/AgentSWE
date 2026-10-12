# BrowseComp Rubric (100 points)

Execution failures receive zero: the harness cannot launch, times out, crashes, emits malformed JSONL, omits a target prediction, or violates a hard resource/security contract. A valid but low-quality result receives an ordinary rubric score; a missing nonessential diagnostic file is not automatically zero. Do not score implementation style, framework choice, prompt wording, intermediate traces, or similarity to a reference agent.

| Dimension | Weight | Observable object |
|---|---:|---|
| Answer correctness | 45 | Exact answer against the evaluator-owned BrowseComp answer |
| Evidence quality | 20 | Explanation grounded in retrieved or supplied evidence |
| Question coverage | 15 | All constraints and entities in the question are handled |
| Output contract | 10 | Required labels, concise answer, confidence and valid JSONL |
| Resource discipline | 10 | Bounded calls, honest failures, no hidden-data access |

For every dimension, use full credit when the final artifact is correct and complete, half credit for a materially useful but incomplete result, and low credit when the result is mostly unusable. Cite concrete row IDs, files, validator output, or measured behavior for every deduction. Reasonable implementation differences that produce the same benchmark outcome must not be penalized.
