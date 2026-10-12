# TerminalBench 2.0 Rubric (100 points)

Execution failures receive zero: the harness cannot launch, times out, crashes, emits malformed JSONL, omits a target prediction, or violates a hard resource/security contract. A valid but low-quality result receives an ordinary rubric score; a missing nonessential diagnostic file is not automatically zero. Do not score implementation style, framework choice, prompt wording, intermediate traces, or similarity to a reference agent.

| Dimension | Weight | Observable object |
|---|---:|---|
| Official task success | 50 | Authoritative TerminalBench validator result |
| Recovery and completeness | 20 | Evidence of iterative commands, tests and correction |
| Protocol validity | 15 | ID preservation, executable agent spec and bounded run report |
| Workspace safety | 10 | Only task container/workspace is changed; no hidden files or secrets |
| Efficiency and honesty | 5 | Reasonable action/time usage and truthful failure status |

For every dimension, use full credit when the final artifact is correct and complete, half credit for a materially useful but incomplete result, and low credit when the result is mostly unusable. Cite concrete row IDs, files, validator output, or measured behavior for every deduction. Reasonable implementation differences that produce the same benchmark outcome must not be penalized.
