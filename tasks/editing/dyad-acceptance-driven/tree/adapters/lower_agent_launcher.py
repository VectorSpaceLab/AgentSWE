#!/usr/bin/env python3
"""Launch the actual Dyad product entry for a lower-agent case.

Default operation executes the headless real-product path. `--execute` remains
a legacy alias for native Electron; native launch is reported as a smoke only
unless a separate driver produces chat/SQLite/Git/Acceptance evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--execute", action="store_true", help="run native Electron (legacy alias)")
    parser.add_argument("--mode", choices=("auto", "native", "headless"), default="auto")
    parser.add_argument("--chat-mode", default="acceptance")
    parser.add_argument("--case-id", default=None)
    parser.add_argument("--artifact", type=Path, default=None)
    parser.add_argument("--native-evidence", type=Path, default=None)
    parser.add_argument("--scenario-public", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--case-deadline-monotonic", type=float)
    args = parser.parse_args()
    # The evaluator's case deadline -- not --timeout -- is what kills this
    # process tree.  Derive every inner budget from it and keep a tail reserve,
    # so this launcher ends through its own timeout branch and still reaches the
    # trajectory and launch-record writes below.  Before this, --timeout stayed
    # at its 600 s default while the case deadline was ~500 s, so the launcher
    # was SIGKILLed with nothing on disk (0919-fw-001 test_004/test_006).
    LAUNCHER_TAIL_RESERVE_SECONDS = 5.0
    if args.case_deadline_monotonic:
        budget = args.case_deadline_monotonic - LAUNCHER_TAIL_RESERVE_SECONDS - time.monotonic()
        args.timeout = max(1, min(args.timeout, int(budget)))
    repo, case, output = args.repository.resolve(), args.case.resolve(), args.output.resolve()
    artifact = args.artifact.resolve() if args.artifact else output.with_suffix(".artifact.json")
    native_evidence = args.native_evidence.resolve() if args.native_evidence else output.with_suffix(".native-evidence.json")
    trajectory = artifact.with_suffix(".trajectory.json")
    case_id = args.case_id or case.stem
    native_ready = all((repo / path).exists() for path in (
        "node_modules/.bin/electron-forge", "node_modules/.bin/vitest"))
    mode = "native" if args.execute else args.mode
    if mode == "auto":
        mode = "native" if os.environ.get("DYAD_ALLOW_NATIVE") == "1" and native_ready else "headless"
    command = ["npm", "start"] if mode == "native" else ["python3", "environment/headless_chat_flow.py"]
    plan = {
        "schema_version": "dyad-lower-agent-launch-v1",
        "product": "dyad",
        "entry": "npm start -> scripts/start-supervisor.mjs -> Electron Forge" if mode == "native" else "repository hybrid harness -> production registerIpcHandlers",
        "chat_entry": "typed chat:stream -> SQLite -> Git -> Acceptance preview/session/attestation",
        "case": str(case),
        "command": command,
        "model": "deepseek-flash",
        "reasoning_effort": "high",
        "broker_endpoint": args.broker_endpoint,
        "candidate_token": "broker-only-placeholder",
        "network_policy": "broker-only; no upstream credential in Candidate",
        "mode": mode,
        "native_dependencies_detected": native_ready,
        "executed": False,
        "artifact": str(artifact),
        "native_evidence": str(native_evidence),
        "trajectory": str(trajectory),
        "trajectory_source": "native evidence model-selected product_action_trajectory",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    if mode == "headless":
        headless = [
            "python3", str(Path(__file__).resolve().parents[1] / "environment" / "headless_chat_flow.py"),
            "--repository", str(repo), "--case", str(case), "--case-id", case_id,
            "--output", str(output), "--artifact", str(artifact),
            "--native-evidence", str(native_evidence),
            "--broker-endpoint", args.broker_endpoint,
            "--chat-mode", args.chat_mode, "--timeout", str(args.timeout),
        ]
        if args.scenario_public is None:
            raise SystemExit("headless execution requires --scenario-public")
        headless.extend(["--scenario-public", str(args.scenario_public.resolve())])
        if args.case_deadline_monotonic:
            headless.extend(["--case-deadline-monotonic", str(args.case_deadline_monotonic)])
        started = time.monotonic()
        # With a case deadline, args.timeout is already the reserved budget and a
        # grace period would push this past the deadline and hand the kill back
        # to the caller, losing the writes below.  Without one, keep the historic
        # grace so this launcher never races the controller it just started.
        headless_timeout = args.timeout if args.case_deadline_monotonic else args.timeout + 30
        try:
            proc = subprocess.run(headless, cwd=repo, env=os.environ.copy(), text=True,
                                  capture_output=True, check=False, timeout=headless_timeout)
            headless_timed_out = False
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(headless, 124, exc.stdout or "", exc.stderr or "timeout")
            headless_timed_out = True
        plan["timed_out"] = headless_timed_out
        if output.is_file():
            plan['headless_runtime_record'] = json.loads(output.read_text())
        plan.update({"command": headless, "executed": True, "exit_code": proc.returncode,
                     "duration_seconds": round(time.monotonic() - started, 3),
                     "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]})
        if native_evidence.is_file():
            try:
                evidence = json.loads(native_evidence.read_text(encoding="utf-8"))
                events = evidence.get("product_action_trajectory") if isinstance(evidence, dict) else None
                if isinstance(events, list):
                    trajectory.write_text(
                        json.dumps({
                            "schema_version": "dyad-lower-agent-trajectory-v1",
                            "case_id": case_id,
                            "source_native_evidence": str(native_evidence),
                            "model_selected_actions_only": True,
                            "events": events,
                        }, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                plan["native_evidence_error"] = "native_evidence_unreadable"
        if artifact.is_file():
            try:
                plan["result"] = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                plan["result_error"] = "artifact_unreadable"
        output.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(plan, sort_keys=True, ensure_ascii=False))
        evidence = {}
        if native_evidence.is_file():
            try:
                evidence = json.loads(native_evidence.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evidence = {}
        return 0 if isinstance(evidence, dict) and evidence.get("real_product") is True else 1
    base_url = args.broker_endpoint.removesuffix("/responses")
    env = {key: value for key, value in os.environ.items() if key not in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY"}}
    env.update({"DYAD_ENGINE_URL": base_url, "OPENAI_BASE_URL": base_url, "AGENTSWE_RESPONSES_BASE_URL": args.broker_endpoint, "GATEWAY_RESPONSES_ENDPOINT": args.broker_endpoint, "DYAD_PRO_API_KEY": "broker-only-placeholder", "OPENAI_API_KEY": "broker-only-placeholder", "DEEPSEEK_API_KEY": "broker-only-placeholder", "AGENTSWE_REQUIRED_MODEL": "deepseek-flash", "AGENTSWE_REQUIRED_REASONING_EFFORT": "high"})
    try:
        proc = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, check=False, timeout=args.timeout)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "timeout")
        timed_out = True
    plan["exit_code"] = proc.returncode
    plan["executed"] = True
    plan["timed_out"] = timed_out
    plan["classification"] = "native_electron_exited" if proc.returncode == 0 else "native_electron_failed"
    plan["behavioral_success"] = False
    plan["evidence_limit"] = "native process exit has no driven chat/session/attestation evidence"
    plan["stdout_tail"] = proc.stdout[-4000:]
    plan["stderr_tail"] = proc.stderr[-4000:]
    output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
