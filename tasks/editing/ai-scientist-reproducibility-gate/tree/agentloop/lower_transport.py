"""Evaluator-owned lower transport contract; no credentials or model requests."""
from contextvars import ContextVar

CASE_DEADLINE = ContextVar("ai_scientist_case_deadline", default=None)
DEADLINE_HEADER = "X-AgentSWE-Lower-Deadline-Monotonic"
COUNTERS = ("calls", "completed_calls", "in_flight_calls", "successful_calls",
            "failures", "client_failures", "provider_failures", "upstream_attempts",
            "usage_unknown_calls", "input_tokens", "output_tokens", "total_tokens")


def snapshot_valid(value, *, fresh=False):
    if not isinstance(value, dict) or value.get("schema_version") != "agentswe-broker-stats-v2":
        return False
    protocol, runtime = value.get("protocol", {}), value.get("runtime", {})
    if not isinstance(protocol, dict) or not isinstance(runtime, dict):
        return False
    if not isinstance(value.get("broker_instance_id"), str) or not value["broker_instance_id"]:
        return False
    expected = {"model": "deepseek-flash", "reasoning_effort": "high", "inner_retries": 0,
                "max_upstream_attempts_per_transport": 1, "redirects_allowed": False,
                "absolute_case_deadline_required": True, "absolute_deadline_seconds_max": 600.0}
    if any(protocol.get(key) != item for key, item in expected.items()):
        return False
    if any(type(runtime.get(key)) is not int or runtime[key] < 0 for key in COUNTERS):
        return False
    return (runtime["calls"] == runtime["completed_calls"] + runtime["in_flight_calls"]
            and runtime["completed_calls"] == runtime["successful_calls"] + runtime["failures"]
            and runtime["failures"] == runtime["client_failures"] + runtime["provider_failures"]
            and runtime["upstream_attempts"] <= runtime["calls"]
            and runtime["usage_unknown_calls"] <= runtime["calls"]
            and runtime["total_tokens"] == runtime["input_tokens"] + runtime["output_tokens"]
            and (not fresh or all(runtime[key] == 0 for key in COUNTERS)))
