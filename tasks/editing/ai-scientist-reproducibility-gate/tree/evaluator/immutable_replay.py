"""Replay a preserved Candidate in new acceptance evidence, without a Builder.

Default mode only prints a plan and reads hashes. --execute explicitly starts
the selected real lower runs; parent-scheduled broker endpoints are required.
Nothing in this script changes historical Candidates, evidence or scores.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentloop"))
from protocol import tree_digest, sha256_file, write_json, read_json


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parser():
    value = argparse.ArgumentParser()
    value.add_argument("--candidate", type=Path, required=True)
    value.add_argument("--expected-candidate-digest", required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--acceptance-cases", nargs="+", default=["test_001", "test_006"])
    value.add_argument("--image", default="sha256:10b0f65061629ca8edab1b33444f49661f1c47e98109fda74072839287630336")
    value.add_argument("--product-action-timeout", type=int, default=180)
    value.add_argument("--case-wall-timeout", type=int, default=600)
    value.add_argument("--lower-broker-endpoint")
    value.add_argument("--result-broker-endpoint")
    value.add_argument("--credential-file", type=Path)
    value.add_argument("--execute", action="store_true")
    value.add_argument("--score", action="store_true")
    return value


# Product containers run with --rm. Docker 29 removes an exited --rm container asynchronously: `docker ps -a`
# and `docker inspect` still show it for several seconds and `docker rm -f` answers "removal of container ... is
# already in progress"; once the daemon has finished, inspect answers "No such object".
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5


def _no_such(text):
    text = text.lower()
    return "no such object" in text or "no such container" in text


def cleanup_owned_runtime(owner_id):
    query = ["docker", "ps", "-aq", "--filter", "label=agentswe.owner=0909-owner-b", "--filter", f"label=agentswe.run_id={owner_id}"]
    owned = subprocess.run(query, text=True, capture_output=True, check=False, timeout=30)
    report = {"owner_id": owner_id, "owned_runtime_only": True, "query_exit_code": owned.returncode, "removals": [], "all_absent": False}
    if owned.returncode != 0:
        report["error"] = "owned-container query failed; absence is unproven"
        return report
    for container_id in owned.stdout.splitlines():
        if not container_id or any(char not in "0123456789abcdef" for char in container_id):
            report["error"] = "invalid container ID from owned query"; return report
        inspected = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=False, timeout=30)
        if inspected.returncode != 0 and _no_such(inspected.stderr):
            # Listed while the daemon was removing it (--rm) and gone since: nothing left to remove.
            report["removals"].append({"container_id": container_id, "already_absent": True, "absent": True}); continue
        if inspected.returncode != 0:
            report["error"] = "container disappeared or inspection failed before exact ownership validation"; return report
        labels = json.loads(inspected.stdout)[0].get("Config", {}).get("Labels", {})
        if labels.get("agentswe.owner") != "0909-owner-b" or labels.get("agentswe.run_id") != owner_id:
            report["error"] = "ownership mismatch; nothing removed"; return report
        removal = subprocess.run(["docker", "rm", "-f", container_id], text=True, capture_output=True, check=False, timeout=30)
        in_progress = removal.returncode != 0 and "already in progress" in removal.stderr.lower()
        after = subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True, check=False, timeout=30)
        wait_until = time.monotonic() + REMOVAL_WAIT_SECONDS
        while in_progress and after.returncode == 0 and time.monotonic() < wait_until:
            time.sleep(REMOVAL_POLL_SECONDS)
            after = subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True, check=False, timeout=30)
        absent = after.returncode != 0 and ("no such object" in after.stderr.lower() or "no such container" in after.stderr.lower())
        row = {"container_id": container_id, "remove_exit_code": removal.returncode, "inspect_after_exit_code": after.returncode, "absent": absent}
        if removal.returncode != 0:
            row.update(remove_stderr=removal.stderr[-500:], removal_in_progress_at_rm=in_progress)
        report["removals"].append(row)
        # Only the daemon's own --rm removal excuses a failed rm; any other refusal still fails.
        if (removal.returncode != 0 and not in_progress) or not absent: report["error"] = "owned container removal unconfirmed"
    remaining = subprocess.run(query, text=True, capture_output=True, check=False, timeout=30)
    report.update(final_query_exit_code=remaining.returncode, remaining_owned_ids=remaining.stdout.splitlines(),
                  all_absent=remaining.returncode == 0 and not remaining.stdout.strip() and "error" not in report)
    return report


def replay(args):
    source, output = args.candidate.resolve(), args.output.resolve()
    selected = tuple(args.acceptance_cases)
    if not 1 <= args.case_wall_timeout <= 600:
        raise ValueError("Create-aligned Candidate case budget must remain within 600 seconds")
    if not 1 <= args.product_action_timeout <= args.case_wall_timeout:
        raise ValueError("product action timeout must be inside the case budget")
    if len(set(selected)) != len(selected) or any(case not in {f"test_{i:03d}" for i in range(1, 7)} for case in selected):
        raise ValueError("acceptance case ids must be unique canonical hidden cases")
    selected = tuple(sorted(selected))
    if tree_digest(source) != args.expected_candidate_digest:
        raise ValueError("preserved Candidate digest differs from the explicit expected digest")
    owner_id = hashlib.sha256(str(output).encode()).hexdigest()[:20]
    plan = {"schema_version": "agentswe-ai-immutable-replay/v1", "mode": "acceptance",
            "selected_cases": list(selected), "source_candidate": str(source),
            "candidate_digest": args.expected_candidate_digest,
            "digest_algorithm": "authoritative Create Code tree SHA256: sorted relative UTF8 paths, files F+8-byte path length+path+8-byte size+bytes; symlinks L+path+target; directories excluded",
            "output": str(output), "image": args.image, "runtime_owner_id": owner_id,
            "builder_requests": 0, "lower_model": "deepseek-flash", "lower_effort": "high",
            "max_action_decisions_per_case": 8, "max_completed_lower_responses_per_case": 9,
            "explicit_transport_retry_limit": 3,
            "broker_inner_retries": 0, "max_upstream_attempts_per_logical_decision": 3,
            "ambiguous_transport_retried": False,
            "product_action_timeout": args.product_action_timeout, "case_wall_timeout": args.case_wall_timeout,
            "result_requests_if_scoreable": len(selected) if args.score else 0,
            "code_requests_if_score": 1 if args.score else 0,
            "formal_result_publishable": False, "dry_run": not args.execute}
    if not args.execute:
        print(json.dumps(plan, indent=2)); return 0
    if not args.lower_broker_endpoint:
        raise ValueError("--execute requires parent-scheduled lower broker endpoint")
    if args.score and (not args.result_broker_endpoint or not args.credential_file):
        raise ValueError("--score requires separate evaluator Result endpoint and credential-file path")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "replay_plan.json", plan)
    write_json(output / "configuration_delta.json", {
        "candidate_case_wall_seconds": 600,
        "effective_case_wall_seconds": args.case_wall_timeout,
        "budget_expansion": False, "model_budget_expansion": False,
        "product_action_deadline_inside_case_budget": args.product_action_timeout,
        "preflight_outside_scored_case": "provider-free isolated prepare diagnostic only; no model response and no acceptance score",
        "runtime_delta": "reuse pinned Python311 Candidate image; selective read-only source/input mounts; network none; cap-drop ALL; no-new-privileges; 4GiB memory /2 CPUs /256pids",
        "technical_reason": "scientific product processes require the task's preinstalled native/Python dependencies; no package download or extra model authority",
        "difficulty_unchanged": "same Candidate source, actual scientific case assets, private fault predicates, lower model/medium,8 decision-turn limit; runtime isolation does not supply recovery decisions",
    })
    lifecycle = output / "lifecycle"
    frozen = lifecycle / "frozen_candidate"
    shutil.copytree(source, frozen, symlinks=True)
    if tree_digest(frozen) != args.expected_candidate_digest or tree_digest(source) != args.expected_candidate_digest:
        raise RuntimeError("Candidate copy/source changed during immutable replay preparation")
    for path in [frozen, *frozen.rglob("*")]:
        if not path.is_symlink(): path.chmod(path.stat().st_mode & ~0o222)
    freeze = {"schema_version": "agentswe-immutable-replay-freeze/v1", "evaluation_mode": "acceptance",
              "frozen_at": now(), "frozen_candidate_path": str(frozen),
              "candidate_path": str(frozen), "candidate_digest": args.expected_candidate_digest,
              "candidate_materialized_digest": args.expected_candidate_digest,
              "hidden_allowed": True, "frozen_tree_read_only": True,
              "source_candidate": str(source), "historical_source_unchanged": True,
              "builder_rerun": False, "dev_ledger_replayed_as_new": False}
    freeze_path = lifecycle / "freeze_manifest.json"
    write_json(freeze_path, freeze)
    freeze_hash = sha256_file(freeze_path)
    entries = []
    case_owners = []
    env = dict(os.environ, AGENTSWE_RUNTIME_OWNER_ID=owner_id, PYTHONDONTWRITEBYTECODE="1")
    try:
        for case_id in selected:
            case_owner = owner_id + "-" + case_id
            case_owners.append(case_owner)
            env["AGENTSWE_RUNTIME_OWNER_ID"] = case_owner
            case_out = lifecycle / "hidden" / case_id
            case_out.mkdir(parents=True)
            command = [sys.executable, str(ROOT / "agentloop/lower_agent_launcher.py"),
                       "--candidate-repository", str(frozen), "--case-file", str(ROOT / "agentloop/cases" / case_id / "case_input.json"),
                       "--output-dir", str(case_out), "--broker-endpoint", args.lower_broker_endpoint,
                       "--timeout", str(args.product_action_timeout), "--image", args.image]
            started = now(); before = tree_digest(frozen)
            env["AGENTSWE_CASE_DEADLINE_MONOTONIC"] = str(time.monotonic() + args.case_wall_timeout)
            with (case_out / "process_stdout.log").open("w") as stdout, (case_out / "process_stderr.log").open("w") as stderr:
                try:
                    completed = subprocess.run(command, stdout=stdout, stderr=stderr, env=env, timeout=args.case_wall_timeout, check=False)
                    exit_code = completed.returncode
                except subprocess.TimeoutExpired:
                    exit_code = 124
                    write_json(case_out / "outer_timeout.json", {"axis": "unresolved", "phase": "whole lower case", "score": None})
                finally:
                    case_cleanup = cleanup_owned_runtime(case_owner)
                    write_json(case_out / "runtime_cleanup.json", case_cleanup)
                    if not case_cleanup["all_absent"]:
                        raise RuntimeError("case-owned runtime cleanup is unconfirmed; refusing to start another case")
            result_path = case_out / "launcher_result.json"
            result = read_json(result_path) if result_path.is_file() else {}
            after = tree_digest(frozen)
            entries.append({"case_id": case_id, "started_at": started, "finished_at": now(),
                            "process_exit_code": exit_code, "result_path": str(result_path),
                            "result_sha256": sha256_file(result_path) if result_path.is_file() else None,
                            "classification": result.get("classification", "missing_launcher_evidence"),
                            "classification_axis": result.get("classification_axis", "unresolved"),
                            "broker": result.get("broker", {}), "real_execution": result.get("real_execution") is True,
                            "frozen_digest_before": before, "frozen_digest_after": after,
                            "frozen_digest_stable": before == after == args.expected_candidate_digest})
            print(json.dumps({"event": "case_finished", "case_id": case_id, "exit_code": exit_code, "classification": entries[-1]["classification"]}), flush=True)
        attestation = {"schema_version": "agentswe-ai-hidden-acceptance-attestation/v1", "evidence_kind": "acceptance",
                       "freeze_manifest": str(freeze_path), "freeze_manifest_sha256": freeze_hash,
                       "frozen_candidate_digest": args.expected_candidate_digest, "expected_cases": list(selected),
                       "executed_cases": [entry["case_id"] for entry in entries], "cases": entries,
                       "complete_inventory": len(entries) == len(selected),
                       "all_cases_materialized": all(Path(entry["result_path"]).is_file() for entry in entries),
                       "all_cases_real": all(entry["real_execution"] for entry in entries),
                       "all_cases_started_after_freeze": all(entry["started_at"] >= freeze["frozen_at"] for entry in entries),
                       "frozen_digest_stable": all(entry["frozen_digest_stable"] for entry in entries) and sha256_file(freeze_path) == freeze_hash,
                       "formal_complete": False, "formal_result_publishable": False}
        write_json(lifecycle / "hidden-after-freeze-attestation.json", attestation)
        if args.score:
            command = [sys.executable, str(ROOT / "evaluator/formal_finalize.py"), "--run-dir", str(output),
                       "--acceptance-cases", *selected, "--result-broker-endpoint", args.result_broker_endpoint,
                       "--credential-file", str(args.credential_file)]
            return subprocess.run(command, env=env, check=False).returncode
        return 0 if attestation["all_cases_materialized"] and attestation["frozen_digest_stable"] else 2
    finally:
        cleanup = [cleanup_owned_runtime(value) for value in case_owners]
        write_json(output / "runtime_cleanup.json", {"owner_id": owner_id, "owned_runtime_only": True,
                                                     "cleanup": cleanup, "all_absent": all(value["all_absent"] for value in cleanup),
                                                     "source_unchanged": tree_digest(source) == args.expected_candidate_digest})
        if not all(value["all_absent"] for value in cleanup):
            raise RuntimeError("acceptance-owned runtime cleanup is unconfirmed")


if __name__ == "__main__": raise SystemExit(replay(parser().parse_args()))
