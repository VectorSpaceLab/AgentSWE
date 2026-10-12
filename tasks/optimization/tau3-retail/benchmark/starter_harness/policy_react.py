"""Editable policy layer for the official tau-bench LLMAgent."""

POLICY = """Follow a policy-aware ReAct loop. Maintain the user's identity and current goal, inspect relevant records before changing them, validate every tool argument against the returned schema, and distinguish read-only from mutating actions. Ask for confirmation before consequential booking, cancellation, payment or profile changes when the conversation requires it. After each tool result, reconcile policy constraints, recover from transient errors with bounded retries, communicate important facts precisely, and terminate when the user goal is satisfied or explicitly deferred. Do not invent data or claim a mutation that was not confirmed by the tool."""


def prediction_for(row: dict) -> dict:
    return {"id": row.get("id"), "agent": {"kind": "tau3_half_duplex", "strategy_prompt": POLICY}}
