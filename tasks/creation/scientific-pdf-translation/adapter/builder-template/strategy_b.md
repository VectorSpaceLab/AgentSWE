## Strategy B prior: self-created development and self-verification

Do not optimize only against `dev_001` and `dev_002`. During this same Builder
session, create a private synthetic suite under `/workspace/self_cases`, run the
current created agent on it, and judge the complete translation bundles yourself.
Keep synthetic cases, outputs, renders, and review notes outside
`/workspace/submission`.

The Builder container provides the same evaluator-owned lower-agent transport used by
official Candidate jobs. Requests are forced to `gpt-5.6-sol/medium`; mounted
credentials are placeholders. Invoke the current agent with the documented private
environment, for example:

    /opt/agentswe/benchmark/agent-create-0804/envs/scientific-pdf-translation-agent-v2/bin/python \
      /workspace/submission/run_agent.py \
      --input /workspace/self_cases/<case>/input.md \
      --output /workspace/self_results/<case>

Create structurally different PDFs rather than paraphrases of the public examples.
Across the session cover native and scan-only pages, multi-column and landscape
layouts, mixed rotations, monolingual and bilingual output, glossary conflicts,
protected equations/identifiers/units, tables/figures/captions/footnotes/references,
long text expansion, low-confidence OCR, split units, and degraded but recoverable
input. Generate small synthetic PDFs and glossaries locally so expected content and
geometry are known.

Before each run, write expected observable invariants. Then parse and render every
output page; compare count/dimensions/rotation and protected tokens; inspect target
glyphs, clipping, overlap, reading order, scientific associations, and glossary
consistency; validate alignment schema/digests/bounds/visible box overlap/confidence;
serve the viewer offline and exercise bidirectional click and keyboard activation at
both viewports. Record results and mechanism-level causes in
`/workspace/self_test_log.md`, then repair general extraction, translation,
reconstruction, alignment, viewer, or validation mechanisms.

Use official submissions as periodic external calibration inside the ten-round
budget: study feedback, update the synthetic coverage matrix, run relevant
self-created cases with the lower agent, self-evaluate, repair, and retain a compact
regression suite. Aim for at least six distinct synthetic cases spanning every major
function. High scores on the two public documents do not prove robustness to new
page structures or languages.
