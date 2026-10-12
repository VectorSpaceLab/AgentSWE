# Clean Basic Search ReAct

This is the editable initial agent for BrowseComp. `run_harness.py` implements a
small multi-turn ReAct loop over the evaluator-owned model, search and visit
broker. The Builder may edit the policy and loop under `agent/`; task data,
broker budgets, model, evaluator and hidden split are outside the editable space.
