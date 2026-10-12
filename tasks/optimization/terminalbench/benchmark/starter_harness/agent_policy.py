"""Editable general Terminus-2 policy, with no task-specific solver."""

BASE_POLICY = """Use the live terminal interactively. First inspect the task and workspace, then make a short plan. Execute one or a few commands, read the complete terminal observation, verify outputs and tests, and recover from errors. Prefer small reversible steps and stop only after the requested artifact is checked. Never assume a command succeeded without observing its result."""


def prediction_for(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "agent": {
            "kind": "terminus2_live",
            "policy": BASE_POLICY,
            "parser": "json",
            "max_turns": 20,
        },
    }
