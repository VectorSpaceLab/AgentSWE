#!/usr/bin/env python3
"""Trusted helpers for an isolated OpenClaw PinchBench runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PINCH_ROOT = Path(os.environ.get("AGENTSWE_SNAPSHOTS", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "snapshots"))) / "pinchbench-skill"
YAML_SITE = Path(os.environ.get("AGENTSWE_SNAPSHOTS", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "snapshots"))) / "pyyaml-site"
PINCH_COMMIT = "819384ae830492365b8363fc26bc2602e73f216d"
OPENCLAW_COMMIT = "0790d9f593ad30c940ed93b5872a8cf6d6f3cf8c"
OPENCLAW_VERSION = "2026.7.1-2"
MODEL = "deepseek-flash"
PROVIDER = "agentswe"
MAX_SPEC_CHARS = 60_000
MAX_RUNTIME_CALLS = 20
MAX_RUNTIME_TOKENS = 200_000


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or relative.startswith(".openclaw/"):
            continue
        digest.update(b"F" + relative.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def load_task(task_id: str) -> Any:
    scripts = PINCH_ROOT / "scripts"
    if YAML_SITE.is_dir() and str(YAML_SITE) not in sys.path:
        sys.path.insert(0, str(YAML_SITE))
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from lib_tasks import TaskLoader

    task_path = PINCH_ROOT / "tasks" / f"{task_id}.md"
    if not task_path.is_file():
        raise FileNotFoundError(f"unknown PinchBench task: {task_id}")
    task = TaskLoader(PINCH_ROOT / "tasks").load_task(task_path)
    if task.task_id != task_id:
        raise ValueError("PinchBench task id mismatch")
    return task


def load_prediction(path: Path, case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 1 or rows[0].get("id") != case_id:
        raise ValueError("prediction row missing or id mismatch")
    agent = rows[0].get("agent")
    if not isinstance(agent, dict):
        raise ValueError("prediction requires an agent object")
    if agent.get("kind") not in {"openclaw", "pinchbench_openclaw", "pinchbench-openclaw"}:
        raise ValueError("agent.kind must be openclaw")
    allowed = {
        "kind",
        "instructions",
        "planning_guidance",
        "recovery_guidance",
        "verification_guidance",
        "tool_profile",
    }
    unsupported = sorted(set(agent) - allowed)
    if unsupported:
        raise ValueError(f"unsupported agent fields: {unsupported}")
    for field in allowed - {"kind", "tool_profile"}:
        value = agent.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"agent.{field} must be a string")
    profile = agent.get("tool_profile", "coding")
    if profile not in {"coding", "minimal"}:
        raise ValueError("agent.tool_profile must be coding or minimal")
    size = sum(len(str(agent.get(field, ""))) for field in allowed)
    if size > MAX_SPEC_CHARS:
        raise ValueError("agent spec exceeds the 60000 character limit")
    normalized = dict(agent)
    normalized["kind"] = "openclaw"
    normalized["tool_profile"] = profile
    return rows[0], normalized


def prepare_workspace(task: Any, workspace: Path, agent: dict[str, Any]) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    for file_spec in task.workspace_files:
        if "content" in file_spec:
            destination = workspace / file_spec["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(file_spec["content"], encoding="utf-8")
            continue
        source = PINCH_ROOT / "assets" / file_spec["source"]
        destination = workspace / file_spec["dest"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    sections = [
        "# Candidate OpenClaw Agent",
        "Work only inside the active workspace. Complete the user's task with the available tools, verify the resulting artifact, and report failures honestly.",
    ]
    labels = (
        ("instructions", "Agent instructions"),
        ("planning_guidance", "Planning guidance"),
        ("recovery_guidance", "Recovery guidance"),
        ("verification_guidance", "Verification guidance"),
    )
    for field, label in labels:
        value = agent.get(field, "").strip()
        if value:
            sections.extend((f"## {label}", value))
    (workspace / "AGENTS.md").write_text("\n\n".join(sections) + "\n", encoding="utf-8")


def openclaw_config(agent: dict[str, Any]) -> dict[str, Any]:
    model_id = f"{PROVIDER}/{MODEL}"
    return {
        "agents": {
            "defaults": {
                "workspace": "/workspace",
                "skipBootstrap": True,
                "model": {"primary": model_id},
                "models": {model_id: {}},
            },
            "list": [
                {
                    "id": "pinchbench",
                    "name": "pinchbench",
                    "workspace": "/workspace",
                    "agentDir": "/state/agents/pinchbench/agent",
                    "model": model_id,
                    "tools": {"profile": agent["tool_profile"]},
                }
            ],
        },
        "gateway": {"mode": "local", "bind": "loopback", "auth": {"mode": "none"}},
        "session": {"dmScope": "per-channel-peer"},
        "tools": {
            "profile": agent["tool_profile"],
            "deny": [
                "message",
                "cron",
                "sessions_send",
                "subagents",
                "agents_list",
                "gateway",
                "nodes",
                "image_generate",
                "video_generate",
                "music_generate",
            ],
            "exec": {"security": "full", "ask": "off", "host": "gateway"},
        },
        "models": {
            "mode": "replace",
            "pricing": {"enabled": False},
            "providers": {
                PROVIDER: {
                    "baseUrl": "http://model-broker:8080/v1",
                    "apiKey": "runtime-only-placeholder",
                    "api": "openai-responses",
                    "request": {"allowPrivateNetwork": True},
                    "models": [
                        {
                            "id": MODEL,
                            "name": MODEL,
                            "reasoning": True,
                            "input": ["text"],
                            "contextWindow": 200_000,
                            "maxTokens": 16_384,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
            },
        },
        "hooks": {"internal": {"entries": {"session-memory": {"enabled": False}}}},
    }


def find_transcript(state: Path) -> Path | None:
    session_dir = state / "agents" / "pinchbench" / "sessions"
    candidates = [
        path
        for path in session_dir.glob("*.jsonl")
        if not path.name.endswith(".trajectory.jsonl")
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def read_transcript(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def transcript_metrics(transcript: list[dict[str, Any]]) -> dict[str, int]:
    metrics = {
        "message_count": 0,
        "tool_calls": 0,
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }
    for event in transcript:
        if event.get("type") != "message" or not isinstance(event.get("message"), dict):
            continue
        metrics["message_count"] += 1
        message = event["message"]
        if message.get("role") != "assistant":
            continue
        metrics["model_calls"] += 1
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        metrics["input_tokens"] += int(usage.get("input", 0) or 0)
        metrics["output_tokens"] += int(usage.get("output", 0) or 0)
        metrics["cache_read_tokens"] += int(usage.get("cacheRead", 0) or 0)
        metrics["cache_write_tokens"] += int(usage.get("cacheWrite", 0) or 0)
        metrics["total_tokens"] += int(usage.get("totalTokens", 0) or 0)
        content = message.get("content") if isinstance(message.get("content"), list) else []
        metrics["tool_calls"] += sum(
            1 for item in content if isinstance(item, dict) and item.get("type") == "toolCall"
        )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.state.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    command = [
        "openclaw",
        "agent",
        "--local",
        "--json",
        "--agent",
        "pinchbench",
        "--session-id",
        f"pinchbench-{args.case_id}",
        "--message-file",
        str(args.prompt),
        "--thinking",
        "medium",
        "--timeout",
        str(args.timeout),
    ]
    environment = os.environ.copy()
    environment["OPENCLAW_STATE_DIR"] = str(args.state)
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=args.workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout + 30,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""

    transcript_path = find_transcript(args.state)
    transcript = read_transcript(transcript_path)
    metrics = transcript_metrics(transcript)
    if transcript_path is not None:
        shutil.copy2(transcript_path, args.output_dir / "transcript.jsonl")
    (args.output_dir / "openclaw.stdout.log").write_text(stdout[-100_000:], encoding="utf-8")
    (args.output_dir / "openclaw.stderr.log").write_text(stderr[-100_000:], encoding="utf-8")
    status = "success"
    if timed_out:
        status = "timeout"
    elif exit_code != 0 or not transcript:
        status = "error"
    execution = {
        "schema_version": "1.0",
        "case_id": args.case_id,
        "task_id": args.task_id,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "execution_time": round(time.monotonic() - started, 3),
        "workspace": str(args.workspace),
        "workspace_digest": tree_digest(args.workspace),
        "transcript": transcript,
        "transcript_digest": sha256(args.output_dir / "transcript.jsonl") if transcript_path else None,
        "usage": metrics,
        "runtime_error_excerpt": stderr[-4_000:] if status != "success" else "",
    }
    write_json(args.output_dir / "execution_result.json", execution)
    for path in args.output_dir.iterdir():
        if path.is_file():
            os.chmod(path, 0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
