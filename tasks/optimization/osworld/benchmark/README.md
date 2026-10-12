# OSWorld Screenshot Agent Optimization

Improve the supplied screenshot ReAct desktop agent. The starter already emits a runnable,
declarative agent policy; submissions refine that policy rather than design a desktop runtime from
an empty artifact. The evaluator executes the policy in isolated, reset OSWorld Ubuntu desktops and
scores only the pinned official OSWorld task reward.

Public development contains three Chrome tasks. Hidden evaluation contains five tasks spanning
Chrome, GIMP, and LibreOffice Calc. The fixed runtime owns screenshots, action parsing, GUI
execution, model credentials, VM lifecycle, task setup, and official evaluation.
