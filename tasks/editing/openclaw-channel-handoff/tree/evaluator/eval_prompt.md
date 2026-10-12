# Evaluator instructions

You receive the current case input, allowed assets, the frozen Candidate's
`agent_result.json` and `run_report.json`, evaluator-captured OpenClaw Gateway
trajectory/state/connector evidence, and the two global rubrics.

First validate the artifact and run contract. Apply the execution-failure rule
when the lower product cannot launch, the run times out, or the artifact is
missing/corrupt. Then score every Agent-loop Result dimension and cite concrete
evidence from the rollout for every deduction. Score the eight Code dimensions
independently from the frozen source and cite file/symbol evidence. Ignore
implementation fashion, prompts, intermediate artifacts, and similarity to the
legacy benchmark.

Return JSON with `classification`, `result_dimensions`, `result_total`,
`code_dimensions`, `code_total`, `evidence`, `major_errors`, and
`overall_assessment`. Use `infrastructure-invalid`/N/A for evaluator/provider/
credential/mount/Docker failures; use Candidate zero/cap for invalid source,
build, artifact contract, or valid lower-agent strategy failures.

