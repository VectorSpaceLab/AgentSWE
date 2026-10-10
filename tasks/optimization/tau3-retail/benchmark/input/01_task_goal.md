# τ³-bench Text Tool-Agent Optimization

You are optimizing the runnable policy-aware ReAct agent already installed in the submission workspace. You are not starting from an empty submission and you are not solving one fixed example.

## Goal

Build a reusable half-duplex customer-service agent that plans multi-turn tool calls, obeys domain policy, confirms mutating actions, recovers from tool errors, and reaches the correct resettable database state plus communication outcome.

## Non-goals

- Do not hard-code the provided development cases or hidden answers.
- Do not submit self-reported rewards as authoritative evidence.
- Do not copy a complete third-party agent or evaluator as the solution.
