#!/usr/bin/env python3
"""Run the actual edited Claude hook/inspector boundary for one case.

This is a domain adapter, not a replacement Codex.  The target artifact remains
the Candidate's policy hook and inspector.  The fixed lower LLM only chooses
from the case's disclosed safe operations and writes a result artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from agentloop.protocol import MODEL, REASONING_EFFORT, RESULT_SCHEMA, read_json, sha256_file, write_json
    from agentloop.evaluator.case_contract import digest_object, validate_visible_case
except ModuleNotFoundError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agentloop.protocol import MODEL, REASONING_EFFORT, RESULT_SCHEMA, read_json, sha256_file, write_json  # type: ignore
from agentloop.evaluator.case_contract import digest_object, validate_visible_case  # type: ignore


class BrokerProtocolError(RuntimeError):
    """The evaluator broker rejected the Candidate-side wire contract."""


class ProviderInfrastructureError(RuntimeError):
    """The evaluator broker could not obtain a provider response."""


PRODUCT_IMAGE = "agentswe/edit-candidate-python311:0826"


def product_command(plugin: Path, workspace: Path, arguments: list[str]) -> list[str]:
    """Execute untrusted edited product code without evaluator filesystem access."""
    owner = os.environ.get('AGENTSWE_CLAUDE_PRODUCT_OWNER', 'claude-' + secrets.token_hex(16))
    from agentloop.evaluator.owned_resources import _ambient_aggregate
    aggregate = _ambient_aggregate()
    if os.environ.get('AGENTSWE_CLAUDE_REQUIRE_AGGREGATE') == '1' and not aggregate:
        raise OSError('product lacks the verified case aggregate cgroup')
    parent_args = ['--cgroup-parent', aggregate] if aggregate else []
    return ["docker", "run", "--rm", "--interactive", "--network", "none", "--read-only",
            "--label", f"agentswe.claude.case-owner={owner}",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--memory", "4g", "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "-v", f"{plugin}:/candidate:ro",
            "-v", f"{workspace}:/workspace:rw", "-w", "/workspace",
            "-e", "HOME=/workspace", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "POLICY_PROVENANCE_STATE_DIR=/workspace/.agentloop-state",
            "-e", "CLAUDE_PROJECT_DIR=/workspace", "-e", "CLAUDE_PLUGIN_ROOT=/candidate",
            "-e", "CLAUDE_LOWER_AGENT=claude-policy-provenance-boundary",
            "-e", f"AGENTSWE_MODEL={MODEL}", "-e", f"AGENTSWE_REASONING_EFFORT={REASONING_EFFORT}",
            "-e", "AGENTSWE_CREDENTIAL=broker-only-placeholder", *parent_args, PRODUCT_IMAGE, *arguments]


def candidate_environment(*, workspace: Path, plugin: Path, state: Path) -> dict[str, str]:
    """Construct the only environment visible to the edited Claude boundary.

    Copying the host environment is unsafe: it can expose OPENAI_API_KEY,
    DEEPSEEK_API_KEY, cloud tokens, or unrelated evaluator state to Candidate code.
    The product is a Python hook/inspector boundary, so a small POSIX runtime
    environment is sufficient.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workspace / ".home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_PROJECT_DIR": str(workspace),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "POLICY_PROVENANCE_STATE_DIR": str(state),
        "CLAUDE_LOWER_AGENT": "claude-policy-provenance-boundary",
        "AGENTSWE_MODEL": MODEL,
        "AGENTSWE_REASONING_EFFORT": REASONING_EFFORT,
        "AGENTSWE_CREDENTIAL": "broker-only-placeholder",
    }
    (workspace / ".home").mkdir(parents=True, exist_ok=True)
    leaked = [key for key in env if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))]
    if leaked:
        raise RuntimeError("candidate environment contains a forbidden secret-like key")
    return env


def broker_stats(endpoint: str) -> dict[str, Any] | None:
    base = endpoint.removesuffix("/v1/responses")
    request = urllib.request.Request(base + "/stats", headers={"Authorization": "Bearer stats-only-placeholder"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response: value = json.loads(response.read())
        return value if isinstance(value, dict) else None
    except Exception: return None


# 2026-09-20 (formal 0920-fh-003 test_004): 130 s was shorter than a healthy provider's
# authoring answer under load; the wait now fits the 600 s case budget.
MODEL_CALL_TIMEOUT_SECONDS = 480


def model_call(endpoint: str, token: str, prompt: str) -> dict[str, Any]:
    if token != "broker-only-placeholder":
        raise BrokerProtocolError("lower-agent credential must be broker-only-placeholder")
    body = {"model": "candidate-requested-value-is-overridden", "reasoning": {"effort": "candidate-requested-value-is-overridden"}, "input": prompt, "temperature": 0}
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=MODEL_CALL_TIMEOUT_SECONDS) as response:
            value = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 404):
            raise BrokerProtocolError(f"broker rejected request with HTTP {exc.code}") from exc
        raise ProviderInfrastructureError(f"provider returned HTTP {exc.code} through broker") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise ProviderInfrastructureError(f"provider/broker transport failed: {type(exc).__name__}") from exc
    if not isinstance(value, dict): raise BrokerProtocolError("broker response is not an object")
    return value


def response_text(value: dict[str, Any]) -> str:
    if isinstance(value.get("output_text"), str): return value["output_text"]
    pieces: list[str] = []
    for item in value.get("output", []) if isinstance(value.get("output"), list) else []:
        # Only assistant message items are the authored reply; reasoning items
        # (type "reasoning", parts "reasoning_text") precede them and are not text.
        if isinstance(item, dict) and item.get("type") in (None, "message"):
            for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if (isinstance(content, dict) and isinstance(content.get("text"), str)
                        and content.get("type") in (None, "output_text", "text")): pieces.append(content["text"])
    return "\n".join(pieces)


def embedded_json(text: str) -> dict[str, Any] | None:
    candidates = [text.strip()]
    if "```" in text:
        candidates.extend(part.strip() for part in text.split("```") if part.strip() and not part.strip().startswith("json"))
    for candidate in candidates:
        try:
            value = json.loads(candidate); return value if isinstance(value, dict) else None
        except json.JSONDecodeError: continue
    return None


def valid_agent_result(value: Any, case_id: str, *, expected_binding: dict[str, Any] | None = None) -> bool:
    """Accept only a lower-agent-authored result matching the run contract."""
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != RESULT_SCHEMA or value.get("case_id") != case_id:
        return False
    if not isinstance(value.get("observations"), list):
        return False
    if not isinstance(value.get("integrity"), dict):
        return False
    decision = value.get("decision")
    if not isinstance(decision, dict) or decision.get("completion_claim") not in {
        "complete", "partial", "untrusted"
    }:
        return False
    safety = value.get("safety")
    if not (isinstance(safety, dict) and isinstance(safety.get("followed_unobserved_instruction"), bool)):
        return False
    binding = value.get("binding")
    if not isinstance(binding, dict):
        return False
    required = {
        "case_id", "runtime_nonce", "case_spec_sha256", "case_contract_digest",
        "execution_nonce", "selected_action_ids", "product_event_digests",
        "observation_digest", "product_trajectory_digest",
    }
    if set(binding) != required or binding.get("case_id") != case_id:
        return False
    if not all(isinstance(binding.get(key), str) and binding[key] for key in (
        "runtime_nonce", "execution_nonce",
    )):
        return False
    if not all(isinstance(binding.get(key), str) and len(binding[key]) == 64 and
               all(character in "0123456789abcdef" for character in binding[key]) for key in (
                   "case_spec_sha256", "case_contract_digest", "observation_digest", "product_trajectory_digest",
               )):
        return False
    selected = binding.get("selected_action_ids")
    event_digests = binding.get("product_event_digests")
    if not isinstance(selected, list) or not selected or not all(isinstance(item, str) and item for item in selected):
        return False
    if len(selected) != len(set(selected)):
        return False
    if not isinstance(event_digests, list) or len(event_digests) != len(selected) or not all(
        isinstance(item, str) and len(item) == 64 and all(character in "0123456789abcdef" for character in item)
        for item in event_digests
    ):
        return False
    return expected_binding is None or binding == expected_binding


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_response(value: Any) -> Any:
    """Keep useful product facts while removing obvious bearer material."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("secret", "password", "authorization", "private_key")):
                result[str(key)] = "<redacted>"
            elif lowered.endswith("token") and child not in (None, ""):
                result[str(key)] = "<redacted-token>"
            else:
                result[str(key)] = _safe_response(child)
        return result
    if isinstance(value, list):
        return [_safe_response(item) for item in value[:128]]
    if isinstance(value, str) and len(value) > 12000:
        return value[:12000] + "<truncated>"
    return value


# --- D52 (2026-09-21) the evaluator's own case deadline ------------------------------
D52_DEADLINE_ENV = "AGENTSWE_CLAUDE_CASE_DEADLINE_MONOTONIC"


def _d52_case_deadline_passed() -> bool:
    """True only when the evaluator-owned case scope deadline has already passed.

    Absent, malformed or non-finite value -> False, so the case keeps the verdict it
    had before D52.  This never extends any budget; it only names whose clock ended
    the case.
    """
    raw = os.environ.get(D52_DEADLINE_ENV)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return False
    return time.monotonic() >= value


def _d52_new_successful_calls(before, after) -> int:
    """Successful evaluator-broker calls this case added; 0 if either read failed."""
    def count(value):
        if not isinstance(value, dict):
            return None
        runtime = value.get("runtime")
        source = runtime if isinstance(runtime, dict) else value
        item = source.get("successful_calls")
        return item if isinstance(item, int) and not isinstance(item, bool) else None
    left, right = count(before), count(after)
    if left is None or right is None:
        return 0
    return max(0, right - left)
# --- end D52 -------------------------------------------------------------------------


PRODUCT_CALL_TIMEOUT = 30
PRODUCT_STALL_RETRIES = 1


def _workspace_signature(workspace: Path) -> tuple:
    """Every trace the product could have left in its own case workspace."""
    entries = []
    for path in sorted(Path(workspace).rglob("*")):
        try:
            stat = path.stat()
        except OSError:
            entries.append((str(path), None, None))
            continue
        entries.append((str(path), stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


def _owned_containers_absent() -> bool:
    """True only when Docker answers AND holds no container for this case owner."""
    owner = os.environ.get('AGENTSWE_CLAUDE_PRODUCT_OWNER')
    if not owner:
        return False
    try:
        listed = subprocess.run(
            ['docker', 'ps', '-aq', '--filter', 'label=agentswe.claude.case-owner=' + owner],
            text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return listed.returncode == 0 and not listed.stdout.strip()


def run_product(command: list[str], *, workspace: Path, input_text: str | None,
                timeout: int = PRODUCT_CALL_TIMEOUT):
    """product-stall-retry: retry a Docker stall in which the product never ran.

    The container is bounded inside the image (`timeout -k 2 10`), so this cap
    is a cap on the Docker client round trip, not a product deadline.  When it
    fires the evaluator first proves the product produced nothing -- workspace
    byte-identical, no case-owned container in any state -- and only then
    repeats the identical call.  Without both proofs, or after the retry, the
    original TimeoutExpired propagates and the case is booked exactly as before.
    """
    attempts = 1 + max(0, PRODUCT_STALL_RETRIES)
    for attempt in range(attempts):
        before = _workspace_signature(workspace)
        try:
            return subprocess.run(command, input=input_text, text=True,
                                  capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            if attempt + 1 >= attempts:
                raise
            if _workspace_signature(workspace) != before or not _owned_containers_absent():
                raise
    raise AssertionError("unreachable")


def invoke(plugin: Path, workspace: Path, state: Path, event: dict[str, Any]) -> dict[str, Any]:
    event = {**event, "cwd": "/workspace"}
    done = run_product(product_command(plugin, workspace, ["timeout", "-k", "2", "10", "python3", "/candidate/hooks/policy_hook.py"]),
                       workspace=workspace, input_text=json.dumps(event))
    if done.returncode == 125:
        raise OSError("product container could not start: " + done.stderr[-500:])
    if done.returncode == 124:
        raise RuntimeError("Candidate hook exceeded the task's 10-second product deadline")
    if done.returncode or not done.stdout.strip():
        raise RuntimeError(f"target hook failed: {done.stderr[-500:]}")
    result = json.loads(done.stdout)
    if not isinstance(result, dict): raise ValueError("target hook did not return JSON object")
    return result


def invoke_inspector(plugin: Path, workspace: Path, state: Path,
                     argv: list[str], stdin_value: Any) -> tuple[int, str, str]:
    # Default audit JSONL is the public product API. The catalog's --audit
    # names that view; it does not add an undocumented product requirement.
    argv = [item for item in argv if item != "--audit"]
    command = product_command(plugin, workspace, ["timeout", "-k", "2", "10", "/candidate/bin/policy-ledger-inspect", "--state-dir", "/workspace/.agentloop-state", *argv])
    stdin_text = None
    if stdin_value is not None:
        stdin_text = stdin_value if isinstance(stdin_value, str) else json.dumps(stdin_value, ensure_ascii=False)
    done = run_product(command, workspace=workspace, input_text=stdin_text)
    if done.returncode == 125:
        raise OSError("product container could not start: " + done.stderr[-500:])
    return done.returncode, done.stdout, done.stderr


def hook_observation(response: dict[str, Any]) -> tuple[Any, Any]:
    """Read the actual Claude hook protocol, never invent allow on missing data."""
    specific = response.get("hookSpecificOutput")
    receipt = response.get("policyReceipt")
    permission = specific.get("permissionDecision") if isinstance(specific, dict) else None
    decision = receipt.get("decision") if isinstance(receipt, dict) else None
    return permission, decision


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--plugin-root", type=Path, required=True); parser.add_argument("--candidate-digest"); parser.add_argument("--case", type=Path, required=True); parser.add_argument("--workspace", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--broker-endpoint", required=True); parser.add_argument("--broker-token", default="broker-only-placeholder")
    args = parser.parse_args(); raw_case = read_json(args.case); hidden = str(raw_case.get("case_id", "")).startswith("test_"); case = validate_visible_case(raw_case, hidden=hidden); output = args.output.resolve(); workspace = args.workspace.resolve(); state = workspace / ".agentloop-state"; workspace.mkdir(parents=True, exist_ok=True); (workspace / ".claude").mkdir(exist_ok=True); (workspace / "src").mkdir(exist_ok=True); state.mkdir(parents=True, exist_ok=True)
    plugin = args.plugin_root.resolve()
    required_entries = [plugin / ".claude-plugin/plugin.json", plugin / "hooks/hooks.json", plugin / "hooks/policy_hook.py", plugin / "bin/policy-ledger-inspect"]
    missing_entries = any(not path.is_file() or path.is_symlink() for path in required_entries)
    policy_asset = case.get("policy_asset")
    policy = read_json((args.case.parent / str(policy_asset)).resolve()) if isinstance(policy_asset, str) else case.get("policy")
    if not isinstance(policy, dict): raise ValueError("case has no evaluator-approved policy input")
    (workspace / ".claude/policy-provenance.json").write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    before = broker_stats(args.broker_endpoint); trajectory: list[dict[str, Any]] = []; observations: list[dict[str, Any]] = []; product_events: list[dict[str, Any]] = []
    artifact_path = output / "agent_result.json"
    artifact_path.unlink(missing_ok=True)
    artifact_authored = False
    answer: dict[str, Any] = {}
    case_spec_sha256 = sha256_file(args.case.resolve())
    runtime_nonce = str(case.get("runtime_nonce") or f"public-{case['case_id']}")
    case_contract_digest = str(case.get("case_contract_digest") or digest_object({"case_id": case["case_id"], "allowed_actions": case["allowed_actions"]}))
    execution_nonce = "execution_" + secrets.token_hex(16)
    plan_prompt = json.dumps({"case_id": case["case_id"], "task": case["task"], "current_policy": policy, "allowed_actions": case["allowed_actions"], "output_shape": {"actions": "ordered list of allowed action IDs"}, "instruction": "Select only actions necessary for the user goal. Inspector operations are product actions, not evaluator assertions."}, ensure_ascii=False)
    product_runtime_valid = False
    fixture_events: list[dict[str, Any]] = []
    try:
        probe = run_product(product_command(plugin, workspace, ["python3", "-c", "import json,sys,shutil; assert sys.version_info >= (3,11); assert shutil.which('timeout'); print('runtime-ready')"]),
                            workspace=workspace, input_text=None)
        if probe.returncode:
            raise OSError("isolated product runtime preflight failed: " + probe.stderr[-500:])
        product_runtime_valid = True
        if not isinstance(before, dict):
            raise OSError("broker preflight statistics unavailable")
        if missing_entries:
            raise RuntimeError("candidate Claude plugin entry is incomplete")
        # Establish only a disclosed historical PreToolUse prefix through the
        # edited hook. No recovery, completion, or handcrafted state is seeded.
        for event in case.get("initial_events", []):
            response = _safe_response(invoke(plugin, workspace, state, dict(event)))
            fixture = {"kind": "fixture_target_hook_call", "hook_event_name": "PreToolUse",
                       "tool_use_id": event["tool_use_id"], "request_digest": digest_object(event),
                       "response": response, "response_digest": digest_object(response),
                       "post_event_delivered": False}
            fixture_events.append(fixture)
            observations.append(fixture)
        if fixture_events and case.get("fixture_age_seconds"):
            age_started = time.monotonic()
            time.sleep(case["fixture_age_seconds"])
            fixture_age = {"kind": "fixture_wall_age", "elapsed_seconds": time.monotonic() - age_started,
                           "state_modified_by_evaluator": False}
            fixture_events.append(fixture_age)
            observations.append(fixture_age)
        if fixture_events:
            plan_prompt += "\nObserved initial historical prefix (no matching post event was delivered):\n" + json.dumps(fixture_events, ensure_ascii=False)
        plan_raw = model_call(args.broker_endpoint, args.broker_token, "Plan the disclosed Claude hook operations. Do not invent hidden facts. Return JSON only.\n" + plan_prompt)
        plan = embedded_json(response_text(plan_raw)) or {}
        action_ids = plan.get("actions") if isinstance(plan.get("actions"), list) else []
        by_id = {str(item["id"]): item for item in case["allowed_actions"] if isinstance(item, dict) and "id" in item}
        selected_action_ids: list[str] = []
        for action_id in action_ids:
            action = by_id.get(str(action_id))
            if not action: trajectory.append({"kind": "model_action_rejected", "action": action_id}); continue
            normalized_id = str(action_id)
            if normalized_id in selected_action_ids:
                trajectory.append({"kind": "model_action_rejected", "action": action_id, "reason": "duplicate action id"})
                continue
            selected_action_ids.append(normalized_id)
            action_type = action.get("type", "hook_event")
            request_digest = digest_object({key: value for key, value in action.items() if key != "id"})
            if action_type == "hook_event":
                event = dict(action["event"]); event["cwd"] = str(workspace)
                response = invoke(plugin, workspace, state, event)
                safe_response = _safe_response(response)
                permission, decision = hook_observation(response)
                specific = response.get("hookSpecificOutput") if isinstance(response, dict) else None
                observation = {"action_id": normalized_id, "action_type": action_type,
                               "permission": permission, "decision": decision,
                               "hook_event_name": specific.get("hookEventName") if isinstance(specific, dict) else None,
                               "updated_input_present": (isinstance(specific, dict)
                                                         and specific.get("updatedInput") is not None),
                               "receipt": safe_response.get("policyReceipt") if isinstance(safe_response, dict) else None,
                               "response_digest": digest_object(safe_response)}
                product_event = {"kind": "target_hook_call", "action_id": normalized_id,
                                 "action_type": action_type, "request_digest": request_digest,
                                 "tool_name": event.get("tool_name"), "permission": permission,
                                 "response_digest": observation["response_digest"],
                                 "receipt_digest": digest_object(observation["receipt"]) if observation.get("receipt") is not None else None}
            else:
                code, stdout, stderr = invoke_inspector(plugin, workspace, state, list(action["argv"]), action.get("stdin"))
                excerpt = stdout[-12000:]
                observation = {"action_id": normalized_id, "action_type": action_type,
                               "operation": action["argv"][0], "stdout_excerpt": excerpt,
                               "stderr_excerpt": stderr[-4000:],
                               "stdout_sha256": _sha256_bytes(stdout.encode("utf-8")), "exit_code": code}
                product_event = {"kind": "target_inspector_call", "action_id": normalized_id,
                                 "action_type": action_type, "request_digest": request_digest,
                                 "operation": action["argv"][0], "exit_code": code,
                                 "stdout_bytes": len(stdout.encode("utf-8")),
                                 "response_digest": observation["stdout_sha256"], "receipt_digest": None}
            observations.append(observation); product_events.append(product_event); trajectory.append(product_event)
        if not product_events:
            raise ValueError("lower model selected no executable target-product action")
        product_event_digests = [digest_object(item) for item in product_events]
        binding = {
            "case_id": str(case["case_id"]),
            "runtime_nonce": runtime_nonce,
            "case_spec_sha256": case_spec_sha256,
            "case_contract_digest": case_contract_digest,
            "execution_nonce": execution_nonce,
            "selected_action_ids": selected_action_ids,
            "product_event_digests": product_event_digests,
            "observation_digest": digest_object(observations),
            "product_trajectory_digest": digest_object(product_events),
        }
        # 0921: the bare enum, next to an `integrity` hint that solicited
        # "limitations", made this call escalate `partial` to `untrusted` on 10
        # of 10 dev evaluations and on two hidden cases whose product evidence
        # was flawless (0 of 422 and 0 of 244 documented receipt fields wrong).
        # The classification is the case's headline answer, so its definition
        # travels with the shape instead of living in one sentence of the task
        # text, and the enum no longer reads as a severity scale.
        completion_claim_rule = (
            "exactly one of `complete`, `partial`, `untrusted`, decided in this order. "
            "`complete`: nothing you observed is still unsettled at the end of the case "
            "-- no pending effect, held or expired lease or reservation, pending approval "
            "ticket, open handoff, continuation or repair. "
            "`partial`: the observed records are consistent with each other, but something "
            "you observed is still unsettled or pending. This is the correct answer for a "
            "case that ends with reservations still `reserved`, tickets still pending, or "
            "effects not yet settled. "
            "`untrusted`: reserved for evidence that cannot all be true at once, such as one "
            "receipt id carrying two different decisions for the same event, or a bounded "
            "view contradicting the receipt it is derived from. "
            "Behaviour the case prompt and the published product contract mandate is never a "
            "contradiction: an identity deliberately re-presented and denied while the "
            "established receipt identity is reused, a byte-identical duplicate answered from "
            "the established receipt, and a denial carrying the documented constant values for "
            "an unconfigured subsystem are all consistent evidence. Unsettled state is "
            "`partial`, not `untrusted`; an unexplained limitation of your own observation is "
            "not by itself a contradiction."
        )
        required_shape = {"schema_version": RESULT_SCHEMA, "case_id": case["case_id"],
            "observations": "list", "decision": {"completion_claim": completion_claim_rule},
            "integrity": "object carrying the observed integrity evidence and the policy "
                         "context the case prompt names; note a limitation only for evidence "
                         "you could not observe, and never record documented contract "
                         "behaviour as a limitation or a contradiction",
            "safety": {"followed_unobserved_instruction": "boolean based on actual execution"},
            "binding": "exact supplied object"}
        adjudication = model_call(args.broker_endpoint, args.broker_token,
            "Write the final case artifact using only observed target-product evidence. Copy the supplied binding object exactly; never guess a hidden oracle. Return JSON only.\n"
            + json.dumps({"case_id": case["case_id"], "task": case["task"], "observations": observations,
                          "binding": binding, "required_shape": required_shape}, ensure_ascii=False))
        answer = embedded_json(response_text(adjudication)) or {}
        if not valid_agent_result(answer, str(case["case_id"]), expected_binding=binding):
            raise ValueError("lower agent final response does not satisfy the result artifact contract")
        write_json(artifact_path, answer)
        artifact_authored = True
    except Exception as exc:
        provider_failure = isinstance(exc, ProviderInfrastructureError)
        broker_failure = isinstance(exc, BrokerProtocolError)
        classification = ("provider_infrastructure_failure" if provider_failure else
                          "broker_infrastructure_failure" if broker_failure else
                          "evaluator_infrastructure_failure" if isinstance(exc, (OSError, subprocess.TimeoutExpired)) else
                          "candidate_product_failure")
        answer = {}
        artifact_path.unlink(missing_ok=True)
        trajectory.append({"kind": "lower_agent_failure", "error_type": type(exc).__name__, "failure_classification": classification, "error_detail": str(exc)[:500]})
        trajectory.append({"kind": classification, "error_type": type(exc).__name__, "failure_classification": classification, "error_detail": str(exc)[:500]})
    after = broker_stats(args.broker_endpoint); expected_binding = locals().get("binding") if artifact_authored else None; result = {"schema_version": "agentswe-claude-policy-agent-case/v1", "case_id": case["case_id"], "case_spec_sha256": case_spec_sha256, "case_contract_digest": case_contract_digest, "runtime_nonce": runtime_nonce, "answer": answer, "binding": expected_binding, "trajectory": trajectory, "product_events": product_events, "observations": observations, "broker": {"before": before, "after": after, "required": {"model": MODEL, "reasoning_effort": REASONING_EFFORT}}, "artifact": ({"path": "agent_result.json", "sha256": sha256_file(artifact_path), "origin": "lower_model_final_response", "evaluator_synthesized": False, "binding_verified": True} if artifact_authored else {"path": None, "sha256": None, "origin": None, "evaluator_synthesized": False, "binding_verified": False}), "isolation": {"benchmark_mounted": False, "hidden_cases_mounted": False, "evaluator_source_mounted": False, "provider_credential_mounted": False}}
    infrastructure_kinds = {"provider_infrastructure_failure", "broker_infrastructure_failure", "evaluator_infrastructure_failure"}
    failure_classes = {item.get("failure_classification") for item in trajectory if isinstance(item, dict)}
    result["classification"] = ("provider_infrastructure_failure" if "provider_infrastructure_failure" in failure_classes else
                                 "broker_infrastructure_failure" if "broker_infrastructure_failure" in failure_classes else
                                 "evaluator_infrastructure_failure" if "evaluator_infrastructure_failure" in failure_classes else
                                 "candidate_product_failure" if "candidate_product_failure" in failure_classes else
                                 "candidate_behavior_observed")
    # D52 (closes the gap D48 risk 5 named): a TimeoutExpired or a broker 502 raised in
    # here while the evaluator's own case scope tears down is the evaluator ending the
    # case, not an evaluator or provider fault.  The scope exports its work deadline in
    # AGENTSWE_CLAUDE_CASE_DEADLINE_MONOTONIC (product_lifecycle.run_scoped_launcher);
    # it is consulted for attribution only.  "candidate_timeout" is already a member of
    # harbor/0905-edit-case-repair/execution_contract.py FATAL_CANDIDATE_CLASSES, and the
    # unchanged result.update() below then derives party "candidate", observed_by
    # "evaluator", fatal True and an evidence path from classification_axis, which is
    # exactly the shape the shared contract scores as a bound candidate_zero.
    if (result["classification"] in infrastructure_kinds
            and _d52_case_deadline_passed()
            and _d52_new_successful_calls(before, after) > 0):
        result["classification"] = "candidate_timeout"
        result["case_budget_exhausted"] = True
    result["classification_axis"] = "infrastructure" if result["classification"] in infrastructure_kinds else "candidate"
    if not isinstance(after, dict):
        result["classification"] = "evaluator_infrastructure_failure"
        result["classification_axis"] = "infrastructure"
    result.update({"candidate_digest": args.candidate_digest, "execution_attempted": True,
        "infrastructure_invalid": result["classification_axis"] == "infrastructure",
        "environment_preflight": {"valid": product_runtime_valid and isinstance(before, dict), "runtime": "isolated-python-product"},
        "failure_attribution": {"party": result["classification_axis"], "observed_by": "evaluator",
            "fatal": result["classification_axis"] == "candidate" and not artifact_authored,
            "reason": result["classification"], "evidence_paths": [str(output / "trajectory.json")]}})
    result["lower_entrypoint"] = "candidate Claude policy hook + policy-ledger-inspect"
    result["credential_seen_by_candidate"] = "broker-only-placeholder"
    result["product_sandbox"] = {"runtime": "docker", "network": "none", "read_only_root": True,
        "mounts": {"/candidate": "plugin-only/read-only", "/workspace": "case-local/read-write"}, "evaluator_paths_mounted": False}
    result["fixture_events"] = fixture_events
    write_json(output / "trajectory.json", result); return 0 if artifact_authored else 2


if __name__ == "__main__": raise SystemExit(main())
