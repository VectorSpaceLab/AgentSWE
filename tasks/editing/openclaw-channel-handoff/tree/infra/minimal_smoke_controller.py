#!/usr/bin/env python3
"""Bounded real OpenClaw pilot: one dev case, no freeze on infrastructure failure."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", type=Path, default=ROOT / "input" / "repository")
    parser.add_argument("--case", type=Path, default=ROOT / "dev_cases" / "dev_001")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "minimal-pilot")
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    broker_port = port(); broker_endpoint = f"http://127.0.0.1:{broker_port}/v1/responses"
    stats_path = output / "broker_stats.json"
    broker_cmd = [sys.executable, str(ROOT / "broker" / "responses_broker.py"), "--bind", "127.0.0.1", "--port", str(broker_port), "--stats-output", str(stats_path)]
    broker = subprocess.Popen(broker_cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    broker_ready = False
    for _ in range(30):
        try:
            get_json(f"http://127.0.0.1:{broker_port}/healthz"); broker_ready = True; break
        except Exception: time.sleep(0.1)
    lower_output = output / args.case.name
    lower_cmd = [sys.executable, str(ROOT / "lower_agent" / "launcher.py"), "--product", str(args.product), "--case", str(args.case), "--broker-endpoint", broker_endpoint, "--output", str(lower_output)]
    if args.runtime: lower_cmd += ["--runtime", str(args.runtime)]
    lower = subprocess.run(lower_cmd, cwd=ROOT, text=True, capture_output=True, check=False, timeout=900)
    stats = get_json(f"http://127.0.0.1:{broker_port}/stats") if broker_ready else {"runtime": {"calls": 0, "failures": 0, "successful_calls": 0}}
    try:
        broker.terminate(); broker.wait(timeout=10)
    except Exception:
        broker.kill(); broker.wait(timeout=5)
    if not stats_path.exists(): stats_path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    result = {
        "schema_version": "openclaw-minimal-pilot-v1",
        "status": "SUCCESS" if lower.returncode == 0 and stats.get("runtime", {}).get("successful_calls", 0) > 0 else "PARTIAL",
        "smoke_scope": {"case": args.case.name, "hidden_started": False, "freeze_started": False},
        "broker": stats,
        "lower_exit_code": lower.returncode,
        "lower_stdout_tail": lower.stdout[-3000:],
        "lower_stderr_tail": lower.stderr[-3000:],
        "evidence": {"lower": str(lower_output), "broker_stats": str(stats_path)},
        "reason": "No upstream Responses endpoint/key was present; broker recorded real provider_unconfigured failures." if stats.get("runtime", {}).get("successful_calls", 0) == 0 else "real production RPC and broker success observed",
        "warning": "This is a bounded infrastructure smoke, not a paper/formal Result.",
    }
    (output / "pilot_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    status = {"status": result["status"], "phase": "B-simple-pilot", "smoke_started": True, "formal_started": False, "real_lower_agent_executed": True, "successful_broker_calls": stats.get("runtime", {}).get("successful_calls", 0), "reason": result["reason"], "evidence": result["evidence"], "promotion_requirement": "Successful evaluator-owned deepseek-flash/medium broker calls plus a completed real lower-agent dev case before READY."}
    (ROOT / "infra" / "pilot_status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
