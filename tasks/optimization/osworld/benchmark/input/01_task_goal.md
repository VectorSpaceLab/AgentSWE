# Task Goal

Optimize the supplied reusable screenshot ReAct policy so it completes unseen OSWorld desktop
tasks more reliably. The agent must ground each action in the current screenshot, recover from
focus, menu, dialog, and timing errors, and verify the requested application state before stopping.

The initial submission is already runnable. Improve its planning, recovery, and verification
policy in place; do not replace the fixed runtime, model, action executor, VM provider, or official
evaluator. Success means changing the real desktop state accepted by the official OSWorld task
validator. A self-reported success claim or `done` action never determines the score.
