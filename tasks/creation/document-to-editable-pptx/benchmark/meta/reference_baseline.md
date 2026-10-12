# Reference Baseline

## Status

The reference implementation and the original benchmark submission were **not executed** during V2 construction.

## Reason

The public reference is an interactive host-agent workflow with optional external providers, design confirmation, and a different output lifecycle. Running an older local shallow clone would not reproduce the pinned evidence commit documented by the original benchmark. A partial run with a different model, provider set, or confirmation path would not create a stable baseline for this one-command V2 task.

The assigned original benchmark contains specifications and cases, not a completed agent or reference deck. It was read to understand the previous abstraction and resource contract; no cases, assets, prompts, or outputs were copied into V2.

## Feasibility Evidence

The repository's documented examples and workflows demonstrate that editable slide text, shapes, charts, tables, diagrams, notes, previews, and PowerPoint export are feasible. The original benchmark establishes a simpler one-command deck-generation interface. Common OOXML libraries and local renderers can inspect the V2 final artifact without depending on the reference implementation.

## Environment Assumptions

V2 gives the builder a dedicated Conda prefix, up to 600 seconds and 4 GiB per case, DeepSeek and GATEWAY model access, a Serper-compatible search layer, direct public-page retrieval with relevant-link traversal, and local browser fallback. The builder owns dependency installation and must validate its agent on the two public cases.

## Baseline Use

Repository and original-benchmark evidence informed only the task boundary and feasibility. No reference output is a required answer, and no score or style comparison is claimed.

## Limitations

- No reference runtime, provider-call count, cost, or quality score was measured.
- No reference deck was rendered in the construction environment.
- V2 adds research, source manifests, formula auditing, native-object checks, notes, and accessibility requirements that the original benchmark did not jointly test.
- Visual quality still needs renderer-backed evaluator judgment.
