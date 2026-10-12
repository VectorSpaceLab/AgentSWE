## Strategy B prior: self-created development and self-verification

Do not optimize only against `dev_001` and `dev_002`. During this same Builder
session, create a private synthetic suite under `/workspace/self_cases`, run the
current created agent on it, and judge the resulting PPTX artifacts yourself. Keep
synthetic inputs, outputs, renders, and notes outside `/workspace/submission`.

The Builder container provides the same evaluator-owned lower-agent transport used by
official Candidate jobs. Requests are forced to `gpt-5.6-sol/medium`; mounted
credentials are placeholders. Invoke the current agent with the documented private
environment, for example:

    /opt/agentswe/benchmark/agent-create-0804/envs/document-to-editable-pptx-agent-v2/bin/python \
      /workspace/submission/run_agent.py \
      --input /workspace/self_cases/<case>/input.md \
      --output /workspace/self_results/<case>

Create structurally different cases rather than paraphrases of the public examples.
Across the session cover long and short sources, mixed headings/lists/tables/images,
quantitative claims, conflicting source authority, different audiences and slide
counts, sparse and dense slides, a chart/table-heavy deck, citations/notes, and an
edge case with a missing optional asset. Use only synthetic or openly usable private
fixtures that you create for this purpose.

Before each run, write expected observable invariants. Afterward parse the OOXML,
verify slide count/relationships/editable object types/notes/source manifest, render
every slide, inspect montage and individual slides for clipping/overlap/legibility,
and check claim/source/number consistency. Record results and mechanism-level causes
in `/workspace/self_test_log.md`; repair extraction, planning, design-system,
generation, provenance, or validation mechanisms rather than hard-coding case text.

Use official submissions as periodic external calibration inside the ten-round
budget: study feedback, update a synthetic coverage matrix, run the lower agent on
relevant self-created cases, self-evaluate, repair, and retain a regression subset.
Aim for at least six distinct synthetic cases spanning all major functions. A deck
that merely exists is not a pass; local parse and full-slide render validation are
mandatory self-test gates.
