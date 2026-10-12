# Requirements And Constraints

The fixed runtime supplies the current 1920x1080 PNG screenshot, public task instruction, recent
trusted action history, and the submitted policy to a locked vision model. It accepts one of:

```text
click, double_click, type, key, hotkey, scroll, wait, done
```

Coordinates, mouse buttons, key names, text length, scroll amount, and wait duration are validated
before execution. Model output cannot execute arbitrary Python or shell. The runtime converts only
validated actions to fixed pyautogui templates. `done` stops the agent but does not replace official
evaluation.

Design the policy to:

- reason from visible state rather than assumed transitions;
- choose stable menus, dialogs, tabs, fields, and application controls;
- avoid destructive retries when focus or timing is uncertain;
- preserve unrelated document and application state;
- save or export when the task requires persistent output;
- verify the requested final state before `done`.

Do not hard-code public case IDs, task answers, coordinates, hidden task guesses, reference
trajectories, evaluator behavior, credentials, or provider details. Do not request private
chain-of-thought. The deliverable may contain only the two editable files and ordinary local
documentation already present in the starter.
