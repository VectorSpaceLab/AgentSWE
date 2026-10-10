# Reference Baseline Evidence

The runtime is pinned to OSWorld commit
`091f5ef1d5544bc74953c77875d5feb5bed30108`, the evaluator-owned Ubuntu qcow2,
and a digest-pinned Docker+KVM direct-kernel provider. The supplied initial agent is the
declarative Screenshot ReAct policy in `starter_harness/`; it is evaluated through exactly the
same model, action parser, GUI executor, budgets, and official OSWorld validators as a candidate.

The following full-chain evidence exists as of 2026-08-14:

- Chrome `dev_001`: starter policy completed the Bing default-search task in 8 actions;
  official `match_in_list` reward `1.0`, score `100`, official/valid/non-infrastructure,
  cleanup verified.
- GIMP `test_002` known-negative: unchanged image; official
  `check_brightness_decrease_and_structure_sim` reward `0`, score `0`,
  official/valid/non-infrastructure ordinary outcome, cleanup verified.
- Calc `test_004` known-negative: unchanged workbook; official `compare_table` reward `0`,
  score `0`, official/valid/non-infrastructure ordinary outcome, cleanup verified.

These records prove the positive Chrome path and the official-zero sensitivity of the GIMP and
Calc evaluator families. They are evaluator construction evidence, not a complete 3-dev starter
baseline and not a hidden score. The benchmark is not release-ready until all eight frozen tasks,
failure-classification controls, Harbor adapter/Eval integration, one-stop stage, and final audit
have accepted artifacts.
