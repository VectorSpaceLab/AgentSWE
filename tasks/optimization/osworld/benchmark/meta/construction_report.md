# Construction Report

- Source: https://github.com/xlang-ai/OSWorld
- Commit: `091f5ef1d5544bc74953c77875d5feb5bed30108`
- Public development cases: 3 (bb5e4c0d-f964-439c-97b6-bdb9747de3f4, 030eeff7-b492-4218-b312-701ec99ee0cc, 2ad9387a-65d8-4e33-ad5b-7580065a27ca)
- Hidden test cases: 5 (af630914-714e-4a24-a7bb-f9af687d3b91, 7a4deb26-d57d-4ea9-9a73-630f66a7b568, 554785e9-4523-4e7a-b8e1-8016f565f56a, 357ef137-7eeb-4c80-a3bb-0951f26a8aff, 42e0a640-4f19-4b28-973d-729602b5a4a7)

The Chrome cases are settings/state tasks with `proxy=false`; no account or live-web dependency is part of this release split.

## Design decisions

Selected distinct applications from `test_small.json`; VM and validator remain evaluator-owned and are not copied into cases.

## Checks

- One uniform JSONL input/output contract is used for every case.
- Dev and hidden IDs are disjoint and hidden gold stays evaluator-owned.
- No source repository prompts, reference answers, validator internals, or credentials are placed in Builder inputs.
- Rubric weights sum to 100 and scores final artifacts only.
- Every case records its source provenance and pinned commit.
