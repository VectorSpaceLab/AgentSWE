#!/usr/bin/env python3
"""Run the pinned PinchBench grade_task() with a brokered Responses judge."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PINCH_ROOT = Path("/pinchbench")
MODEL = "deepseek-flash"
MAX_JUDGE_ATTEMPTS = 3


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def responses_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            # Lite: DeepSeek returns a reasoning item before the message; read only message items.
            if not isinstance(item, dict) or item.get("type") not in (None, "message"):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks).strip()


class BrokerJudge:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/") + "/responses"
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, prompt: str, model: str, timeout_seconds: float = 120.0) -> dict[str, Any]:
        del model
        started = time.monotonic()
        last_error = "judge_unknown"
        for attempt in range(1, MAX_JUDGE_ATTEMPTS + 1):
            payload = {
                "model": MODEL,
                "input": [
                    {"role": "system", "content": "Return only the requested strict JSON object."},
                    {"role": "user", "content": prompt},
                ],
                "max_output_tokens": 4096,
            }
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": "Bearer judge-only-placeholder",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    body = json.loads(response.read().decode("utf-8"))
                text = responses_text(body)
                if not text:
                    raise ValueError("empty_responses_text")
                self.calls.append(
                    {
                        "attempt": attempt,
                        "ok": True,
                        "runtime_seconds": round(time.monotonic() - started, 3),
                    }
                )
                return {"status": "success", "text": text}
            except urllib.error.HTTPError as exc:
                last_error = f"judge_http_{exc.code}"
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = f"judge_transport_{type(exc).__name__}"
            if attempt < MAX_JUDGE_ATTEMPTS:
                time.sleep(min(4.0, float(2 ** (attempt - 1))))
        self.calls.append(
            {
                "attempt": attempt,
                "ok": False,
                "error": last_error,
                "runtime_seconds": round(time.monotonic() - started, 3),
            }
        )
        return {"status": "error", "text": "", "error": last_error}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--execution-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-base-url", default="http://model-broker:8080/v1")
    args = parser.parse_args()
    scripts = PINCH_ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    from lib_tasks import TaskLoader
    import lib_grading

    task = TaskLoader(PINCH_ROOT / "tasks").load_task(
        PINCH_ROOT / "tasks" / f"{args.task_id}.md"
    )
    execution = json.loads(args.execution_result.read_text(encoding="utf-8"))
    judge = BrokerJudge(args.broker_base_url)
    original = lib_grading.call_judge_api
    lib_grading.call_judge_api = judge
    try:
        grade = lib_grading.grade_task(
            task=task,
            execution_result=execution,
            skill_dir=PINCH_ROOT,
            judge_model=f"openai/{MODEL}",
            judge_backend="api",
            judge_timeout_seconds=240,
            verbose=False,
        )
    finally:
        lib_grading.call_judge_api = original

    judge_required = task.grading_type in {"hybrid", "llm_judge"}
    judge_failed = judge_required and (not judge.calls or not any(call.get("ok") for call in judge.calls))
    result = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "grading_type": task.grading_type,
        "score": float(grade.score),
        "max_score": float(grade.max_score),
        "breakdown": grade.breakdown,
        "notes": grade.notes,
        "judge_required": judge_required,
        "judge_infrastructure_failure": judge_failed,
        "judge_calls": judge.calls,
        "official_grade_task": True,
    }
    write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
