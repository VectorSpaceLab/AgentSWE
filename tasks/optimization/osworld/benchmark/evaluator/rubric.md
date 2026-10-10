# OSWorld-Verified Rubric (100 points)

Execution failures receive zero: the harness cannot launch, times out, crashes, emits malformed JSONL, omits a target prediction, or violates a hard resource/security contract. A valid but low-quality result receives an ordinary rubric score; a missing nonessential diagnostic file is not automatically zero. Do not score implementation style, framework choice, prompt wording, intermediate traces, or similarity to a reference agent.

| Dimension | Weight | Observable object |
|---|---:|---|
| Validator success | 50 | Official OSWorld validator result |
| Action correctness | 20 | Actions lead to the requested final state |
| Observation/recovery | 15 | Screenshot-grounded decisions and recovery |
| Protocol/resource validity | 10 | Valid live spec, bounded actions, isolated VM |
| Honest completion | 5 | Done only after observable verification |

For every dimension, use full credit when the final artifact is correct and complete, half credit for a materially useful but incomplete result, and low credit when the result is mostly unusable. Cite concrete row IDs, files, validator output, or measured behavior for every deduction. Reasonable implementation differences that produce the same benchmark outcome must not be penalized.
