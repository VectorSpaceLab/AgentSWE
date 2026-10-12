"""Own one parent-approved medium broker and two immutable acceptance cases."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "evaluator/lower_responses_broker.py"
sys.path.insert(0, str(ROOT / "agentloop"))
from protocol import tree_digest, sha256_file, write_json
from lower_transport import snapshot_valid


def stats(endpoint):
    request = urllib.request.Request(endpoint + "/stats", headers={"Authorization": "Bearer stats-only-placeholder"})
    with urllib.request.urlopen(request, timeout=10) as response: return json.loads(response.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--expected-candidate-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists(): raise ValueError("acceptance output already exists")
    if tree_digest(args.candidate.resolve()) != args.expected_candidate_digest: raise ValueError("source digest changed")
    control = output.with_name(output.name + "-control")
    control.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    command = [sys.executable, str(BROKER), "--credential-file", str(args.credential_file.resolve()),
               "--bind", "127.0.0.1", "--port", str(port), "--stats-output", str(control / "broker_stats.json")]
    child = None
    metadata = {"schema_version": "agentswe-ai-acceptance-broker-lifecycle/v1", "controller_pid": os.getpid(),
                "model": "deepseek-flash", "effort": "high", "role": "lower-only",
                "broker_script": str(BROKER), "broker_sha256": sha256_file(BROKER),
                "endpoint": endpoint + "/v1/responses", "bind": "127.0.0.1",
                "candidate_network": "none", "candidate_credential": "placeholder-only",
                "accepted_case_ids": ["test_001", "test_006"], "builder_requests": 0,
                "semantic_judges_started": False, "candidate_digest": args.expected_candidate_digest}
    with (control / "broker.stdout.log").open("w") as stdout, (control / "broker.stderr.log").open("w") as stderr:
        broker = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        metadata["broker_pid"] = broker.pid
        write_json(control / "metadata.json", metadata)
        try:
            for _ in range(60):
                if broker.poll() is not None: raise RuntimeError("broker process exited during startup")
                try:
                    initial = stats(endpoint)
                    break
                except Exception: time.sleep(0.25)
            else: raise RuntimeError("medium broker startup timed out")
            if not snapshot_valid(initial, fresh=True): raise RuntimeError("broker is not a fresh single-upstream medium instance")
            write_json(control / "broker_before.json", initial)
            replay = [sys.executable, str(ROOT / "evaluator/immutable_replay.py"), "--candidate", str(args.candidate.resolve()),
                      "--expected-candidate-digest", args.expected_candidate_digest, "--output", str(output),
                      "--acceptance-cases", "test_001", "test_006", "--case-wall-timeout", "600",
                      "--product-action-timeout", "180", "--execute", "--lower-broker-endpoint", endpoint + "/v1/responses"]
            child = subprocess.Popen(replay, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            metadata["replay_pid"] = child.pid
            write_json(control / "metadata.json", metadata)
            print(json.dumps({"event": "acceptance_started", **metadata}), flush=True)
            result = child.wait(timeout=1320)
            metadata["replay_exit_code"] = result
            return result
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try: child.wait(timeout=30)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=10)
            try: write_json(control / "broker_after.json", stats(endpoint))
            except Exception as exc: metadata["final_stats_error"] = type(exc).__name__
            broker.terminate()
            try: broker.wait(timeout=15)
            except subprocess.TimeoutExpired: broker.kill(); broker.wait(timeout=10)
            metadata["broker_stopped"] = broker.poll() is not None
            metadata["source_unchanged"] = tree_digest(args.candidate.resolve()) == args.expected_candidate_digest
            write_json(control / "metadata.json", metadata)
            if output.is_dir(): write_json(output / "broker_lifecycle.json", metadata)
            print(json.dumps({"event": "broker_cleanup", "broker_pid": broker.pid, "broker_stopped": metadata["broker_stopped"], "source_unchanged": metadata["source_unchanged"]}), flush=True)


if __name__ == "__main__": raise SystemExit(main())
