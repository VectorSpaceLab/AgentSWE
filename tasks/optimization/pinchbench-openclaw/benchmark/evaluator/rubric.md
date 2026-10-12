# PinchBench Rubric (100 points)

Execution failures receive zero: the harness cannot launch, times out, crashes, emits malformed JSONL, omits a target prediction, or violates a hard resource/security contract. A valid but low-quality result receives an ordinary rubric score; a missing nonessential diagnostic file is not automatically zero. Do not score implementation style, framework choice, prompt wording, intermediate traces, or similarity to a reference agent.

| Dimension | Weight | Observable object |
|---|---:|---|
| Task outcome | 40 | Authoritative workspace grader result |
| Requirement coverage | 25 | All requested files, fields and constraints |
| Planning/recovery | 15 | Multi-step decomposition and recovery from tool errors |
| Artifact usability | 10 | Readable, parseable, practically useful final files |
| Safety/resource honesty | 10 | Workspace boundary, bounded calls, truthful report |

For every dimension, use full credit when the final artifact is correct and complete, half credit for a materially useful but incomplete result, and low credit when the result is mostly unusable. Cite concrete row IDs, files, validator output, or measured behavior for every deduction. Reasonable implementation differences that produce the same benchmark outcome must not be penalized.
