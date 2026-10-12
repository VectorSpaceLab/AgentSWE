# τ³-bench Text Tool-Agent Optimization

## v3 optimization protocol

The Builder optimizes a supplied policy-aware ReAct starter. Development uses the first 20 ordered tasks from the official retail train split, and hidden evaluation uses all 40 tasks from the official retail test split. See `task_contract.json` for editable and fixed spaces.

All 60 rows map to distinct official retail tasks from the pinned τ³-bench source. The benchmark uses text/half-duplex only, with a fresh resettable local simulator and official end-state reward for every row.
