#!/usr/bin/env python3
"""Trusted screenshot loop and official OSWorld evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from osworld_agent import (
    ActionError,
    EpisodeLimits,
    action_to_pyautogui,
    build_model_input,
    image_digest,
    validate_action,
)


class InfrastructureError(RuntimeError):
    pass


class OrdinaryAgentError(RuntimeError):
    pass


def configure_localhost_proxy_bypass() -> None:
    """Keep the outbound proxy, but never proxy the VM's local CDP endpoint.

    The OSWorld VM exposes Chrome DevTools on a host-local forwarded port.  The
    host environment intentionally has HTTP(S)_PROXY configured for task web
    access, but Playwright's Node helper also inherits those variables.  Without
    an explicit localhost bypass, connect_over_cdp sends /json/version through
    the proxy and receives a proxy-generated HTTP 400 instead of Chrome's JSON.
    """
    additions = ("127.0.0.1", "localhost")
    for name in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(name, "")
        values = [part.strip() for part in current.split(",") if part.strip()]
        for value in additions:
            if value not in values:
                values.append(value)
        os.environ[name] = ",".join(values)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_size(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise InfrastructureError("screenshot_not_png")
    width, height = struct.unpack(">II", payload[16:24])
    if not 640 <= width <= 3840 or not 480 <= height <= 2160:
        raise InfrastructureError("screenshot_dimensions_out_of_range")
    return width, height


def response_text(value: Any) -> str:
    if not isinstance(value, dict):
        raise InfrastructureError("model_response_not_object")
    if isinstance(value.get("output_text"), str):
        return value["output_text"]
    messages: list[str] = []
    for item in value.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        chunks: list[str] = []
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        if chunks:
            messages.append("".join(chunks))
    if not messages:
        raise InfrastructureError("model_response_missing_output_text")
    return messages[-1]


def request_action(
    broker_url: str,
    broker_token: str,
    model_input: list[dict[str, Any]],
    width: int,
    height: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = urllib.request.Request(
        broker_url.rstrip("/") + "/v1/osworld/action",
        data=json.dumps({"input": model_input, "screen": {"width": width, "height": height}}).encode(),
        headers={"Authorization": f"Bearer {broker_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=260) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            error = json.loads(body).get("error", {})
        except (ValueError, AttributeError):
            error = {}
        if exc.code == 429 and error.get("type") == "budget_exceeded":
            raise OrdinaryAgentError(str(error.get("message", "model_budget_exceeded"))) from exc
        raise InfrastructureError(f"model_broker_http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise InfrastructureError(f"model_broker_transport_{type(exc).__name__}") from exc
    try:
        parsed = json.loads(response_text(payload))
        action = validate_action(parsed, width, height)
    except (json.JSONDecodeError, ActionError) as exc:
        raise OrdinaryAgentError(f"malformed_model_action:{exc}") from exc
    return action, payload


def recover_screenshot(env: Any, observation: Any, attempts: int = 3) -> tuple[dict[str, Any], bytes]:
    """Retry only the read-only observation after a transient screenshot outage."""
    current = observation
    for attempt in range(attempts + 1):
        screenshot = current.get("screenshot") if isinstance(current, dict) else None
        if isinstance(screenshot, bytes) and screenshot:
            try:
                png_size(screenshot)
                return current, screenshot
            except InfrastructureError:
                pass
        if attempt == attempts:
            break
        time.sleep(5)
        try:
            current = env._get_obs()
        except Exception:
            current = None
    raise InfrastructureError("screenshot_unavailable_after_read_only_retries")


def official_result(
    *, case_id: str, native_task_id: str, task_digest: str, prediction_digest: str,
    vm_image_digest: str, reward: float, termination_reason: str, trajectory: list[dict[str, Any]],
    wall_seconds: float, final_screenshot_digest: str, validator: Any, cleanup_verified: bool,
) -> dict[str, Any]:
    bounded_reward = max(0.0, min(1.0, float(reward)))
    return {
        "schema_version": "1.0",
        "benchmark": "osworld-desktop-agent-optimization-v1",
        "case_id": case_id,
        "native_task_id": native_task_id,
        "official_evaluation": True,
        "validity_gate": True,
        "infrastructure_failure": False,
        "ordinary_agent_failure": bounded_reward == 0.0,
        "score": round(100 * bounded_reward),
        "reward": bounded_reward,
        "termination_reason": termination_reason,
        "action_count": len(trajectory),
        "wall_seconds": round(wall_seconds, 3),
        "validator": validator,
        "task_digest": task_digest,
        "prediction_digest": prediction_digest,
        "trajectory_digest": hashlib.sha256(json.dumps(trajectory, sort_keys=True).encode()).hexdigest(),
        "final_screenshot_digest": final_screenshot_digest,
        "vm_image_digest": vm_image_digest,
        "source_commit": "091f5ef1d5544bc74953c77875d5feb5bed30108",
        "provider": "docker-kvm-uefi-fresh-container",
        "cleanup_verified": cleanup_verified,
        "errors": [],
    }


def infrastructure_result(case_id: str, native_task_id: str, error: str, **evidence: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "benchmark": "osworld-desktop-agent-optimization-v1",
        "case_id": case_id, "native_task_id": native_task_id,
        "official_evaluation": False, "validity_gate": False,
        "infrastructure_failure": True, "ordinary_agent_failure": False,
        "score": 0, "reward": 0, "round_consumed": False,
        "errors": [error], **evidence,
    }


def ordinary_result(case_id: str, native_task_id: str, error: str, **evidence: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0", "benchmark": "osworld-desktop-agent-optimization-v1",
        "case_id": case_id, "native_task_id": native_task_id,
        "official_evaluation": True, "validity_gate": True,
        "infrastructure_failure": False, "ordinary_agent_failure": True,
        "score": 0, "reward": 0, "round_consumed": True,
        "errors": [error], **evidence,
    }


def run_episode(args: argparse.Namespace) -> dict[str, Any]:
    configure_localhost_proxy_bypass()
    metadata = json.loads(args.task_metadata.read_text(encoding="utf-8"))
    record = metadata.get(args.task_id)
    if not isinstance(record, dict) or record.get("id") != args.task_id:
        raise InfrastructureError("task_metadata_mapping_invalid")
    task = record.get("task")
    if not isinstance(task, dict) or task.get("id") != args.task_id:
        raise InfrastructureError("official_task_missing")
    row = json.loads(args.prediction.read_text(encoding="utf-8"))
    agent = row["agent"]
    limits = EpisodeLimits()
    if args.proxy_config:
        os.environ["PROXY_CONFIG_FILE"] = str(args.proxy_config)
    try:
        from osworld_provider import install_provider_override
        from desktop_env.desktop_env import DesktopEnv
        install_provider_override(
            args.run_token,
            str(args.kernel),
            str(args.initrd),
            str(args.provider_entrypoint),
        )
    except Exception as exc:
        raise InfrastructureError(f"osworld_import_failed:{type(exc).__name__}") from exc

    trajectory: list[dict[str, Any]] = []
    env: Any = None
    cleanup_verified = False
    termination_reason = "action_budget"
    output_dir: Path = args.output_dir
    try:
        try:
            env = DesktopEnv(
                provider_name="docker", path_to_vm=str(args.vm_image), snapshot_name="init_state",
                action_space="pyautogui", screen_size=(1920, 1080), headless=True,
                require_a11y_tree=False, require_terminal=False, os_type="Ubuntu",
                enable_proxy=bool(args.proxy_config), cache_dir=str(args.fixture_cache),
            )
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:300]
            raise InfrastructureError(f"provider_start_failed:{type(exc).__name__}:{detail}") from exc
        try:
            observation = env.reset(task_config=task)
        except Exception as exc:
            raise InfrastructureError(f"official_setup_failed:{type(exc).__name__}") from exc
        # VM boot and official task setup are evaluator infrastructure. The agent's
        # 900-second wall budget starts only once its first observation is ready.
        started = time.monotonic()
        for step in range(1, limits.max_actions + 1):
            elapsed = time.monotonic() - started
            if elapsed >= limits.max_wall_seconds:
                termination_reason = "wall_timeout"
                raise OrdinaryAgentError("episode_wall_timeout")
            observation, screenshot = recover_screenshot(env, observation)
            width, height = png_size(screenshot)
            screenshot_path = output_dir / "screenshots" / f"step_{step:03d}.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_bytes(screenshot)
            model_input = build_model_input(task["instruction"], agent, screenshot, trajectory, width, height)
            action, response = request_action(args.broker_url, args.broker_token, model_input, width, height)
            entry = {
                "step": step, "observation_sha256": image_digest(screenshot),
                "action": action, "model_response_sha256": hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest(),
            }
            trajectory.append(entry)
            if action["kind"] == "done":
                termination_reason = "agent_done"
                break
            command = action_to_pyautogui(action)
            try:
                observation, _, _, _ = env.step(command, pause=2)
                entry["result"] = "executed"
            except Exception as exc:
                raise InfrastructureError(f"action_server_failed:{type(exc).__name__}") from exc
        else:
            raise OrdinaryAgentError("episode_action_budget_exceeded")
        try:
            final_observation = env._get_obs()
        except Exception:
            final_observation = None
        final_observation, final_screenshot = recover_screenshot(env, final_observation)
        (output_dir / "final.png").write_bytes(final_screenshot)
        try:
            reward = float(env.evaluate())
        except Exception as exc:
            raise InfrastructureError(f"official_evaluator_failed:{type(exc).__name__}") from exc
        result = official_result(
            case_id=args.case_id, native_task_id=args.task_id,
            task_digest=sha256(args.task_metadata), prediction_digest=sha256(args.prediction),
            vm_image_digest=args.vm_image_digest, reward=reward,
            termination_reason=termination_reason, trajectory=trajectory,
            wall_seconds=time.monotonic() - started,
            final_screenshot_digest=image_digest(final_screenshot),
            validator=task.get("evaluator", {}).get("func"), cleanup_verified=False,
        )
        write_json(output_dir / "trajectory.json", trajectory)
        return result
    finally:
        if env is not None:
            try:
                env.close()
                cleanup_verified = True
            except Exception as exc:
                raise InfrastructureError(f"provider_cleanup_failed:{type(exc).__name__}") from exc
        args.cleanup_state["verified"] = cleanup_verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-metadata", type=Path, required=True)
    parser.add_argument("--prediction", type=Path, required=True)
    parser.add_argument("--vm-image", type=Path, required=True)
    parser.add_argument("--vm-image-digest", required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initrd", type=Path, required=True)
    parser.add_argument("--provider-entrypoint", type=Path, required=True)
    parser.add_argument("--fixture-cache", type=Path, required=True)
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--broker-token", required=True)
    parser.add_argument("--run-token", required=True)
    parser.add_argument("--proxy-config", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.cleanup_state = {"verified": False}
    args.episode_evidence = {}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_episode(args)
        if args.cleanup_state["verified"]:
            result["cleanup_verified"] = True
        else:
            result = infrastructure_result(args.case_id, args.task_id, "cleanup_proof_missing")
    except OrdinaryAgentError as exc:
        result = ordinary_result(
            args.case_id, args.task_id, str(exc),
            cleanup_verified=bool(args.cleanup_state["verified"]),
            **args.episode_evidence,
        )
    except InfrastructureError as exc:
        result = infrastructure_result(args.case_id, args.task_id, str(exc))
    except Exception as exc:
        result = infrastructure_result(args.case_id, args.task_id, f"unexpected_runtime_failure:{type(exc).__name__}")
    write_json(args.output_dir / "native_result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
