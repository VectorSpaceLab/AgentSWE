#!/usr/bin/env python3
"""Evaluator-owned public-case control; never used as hidden candidate evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def call(argv: list[str], cwd: Path, *, env: dict[str, str] | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, input=input_text, text=True, capture_output=True, check=False)


def git(repo: Path, *args: str, env: dict[str, str] | None = None, input_text: str | None = None) -> str:
    done = call(["git", *args], repo, env=env, input_text=input_text)
    if done.returncode:
        raise RuntimeError(done.stderr.strip())
    return done.stdout


def oid(repo: Path, ref: str) -> str | None:
    done = call(["git", "rev-parse", "-q", "--verify", ref], repo)
    return done.stdout.strip() or None


def common_dir(repo: Path) -> Path:
    value = Path(git(repo, "rev-parse", "--git-common-dir").strip())
    return value if value.is_absolute() else (repo / value).resolve()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def store_object(state: Path, value: dict[str, Any]) -> str:
    data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(data).hexdigest()
    path = state / "objects" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return "sha256:" + digest


def empty_test(argv: list[str]) -> dict[str, Any]:
    return {"argv": argv, "state": "pending", "exit_code": None, "stdout_bytes": 0, "stderr_bytes": 0, "stdout_sha256": None, "stderr_sha256": None}


def completed_test(argv: list[str], done: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    stdout, stderr = done.stdout.encode(), done.stderr.encode()
    return {
        "argv": argv, "state": "passed" if done.returncode == 0 else "failed", "exit_code": done.returncode,
        "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }


def save(path: Path, ledger: dict[str, Any]) -> None:
    ledger["generation"] += 1
    ledger["event_count"] += 1
    atomic_json(path, ledger)


def lease_state(ledger: dict[str, Any]) -> str:
    if ledger["cleanup"]["admissions_released"]:
        return "released"
    return "expired" if int(time.time() * 1000) >= ledger["expires"] else "active"


def response(ledger: dict[str, Any], operation: str, *, state: str | None = None, reason: str | None = None) -> dict[str, Any]:
    current_lease = lease_state(ledger)
    digest = hashlib.sha256(json.dumps(ledger, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    admissions = [{
        "repository_id": item["id"], "identity": item["identity"], "state": current_lease,
        "fence": ledger["fence"], "expires_at_unix_ms": None if current_lease == "released" else ledger["expires"],
    } for item in ledger["repositories"]]
    return {
        "schema_version": 3, "operation": operation, "plan_id": ledger["plan_id"], "transaction_id": ledger["transaction_id"],
        "state": state or ledger["state"], "reason": ledger["reason"] if reason is None else reason, "repo": ledger["repo"],
        "coordination": {
            "owner_id": ledger["owner_id"], "lease_token": ledger["token"], "fence": ledger["fence"],
            "lease_state": current_lease, "expires_at_unix_ms": None if current_lease == "released" else ledger["expires"],
            "admissions": admissions,
        },
        "repositories": [{key: value for key, value in item.items() if key in {"id", "repo", "base_revision", "base_snapshot", "state", "candidate_commit", "tree_oid", "prepare_digest", "quarantine_digest", "objects_promoted"}} for item in ledger["repositories"]],
        "subtasks": ledger["subtasks"], "integration_tests": ledger["integration_tests"],
        "decision": ledger["decision"], "publication": ledger["publication"], "cleanup": ledger["cleanup"],
        "ledger": {"schema_version": 3, "generation": ledger["generation"], "event_count": ledger["event_count"], "digest": "sha256:" + digest},
        "errors": [],
    }


def rejected_response(request: dict[str, Any], reason: str) -> dict[str, Any]:
    repositories = []
    admissions = []
    for item in (request.get("plan") or {}).get("repositories", []):
        identity = str(common_dir(Path(item["path"]))) if Path(item["path"]).exists() else item["path"]
        repositories.append({
            "id": item["id"], "repo": item["path"], "base_revision": item["base_revision"], "base_snapshot": None,
            "state": "blocked", "candidate_commit": None, "tree_oid": None, "prepare_digest": None,
            "quarantine_digest": None, "objects_promoted": False,
        })
        admissions.append({"repository_id": item["id"], "identity": identity, "state": "active", "fence": 1, "expires_at_unix_ms": None})
    empty_digest = "sha256:" + hashlib.sha256(reason.encode()).hexdigest()
    return {
        "schema_version": 3, "operation": request["operation"], "plan_id": request["plan_id"], "transaction_id": "rejected-" + uuid.uuid4().hex,
        "state": "blocked", "reason": reason, "repo": request["repo"],
        "coordination": {"owner_id": None, "lease_token": None, "fence": 1, "lease_state": "expired", "expires_at_unix_ms": None, "admissions": admissions},
        "repositories": repositories, "subtasks": [], "integration_tests": [],
        "decision": {"state": "none", "digest": None, "participant_order": [], "completed_participants": []},
        "publication": {"state": "pending", "targets": []},
        "cleanup": {"state": "preserved", "remaining_worktrees": [], "remaining_branches": [], "admissions_released": False},
        "ledger": {"schema_version": 3, "generation": 1, "event_count": 1, "digest": empty_digest}, "errors": [],
    }


def emit(path: Path, value: dict[str, Any], exit_code: int = 0) -> int:
    text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, path)
    print(text)
    return exit_code


def quarantine_env(repo: Path, quarantine: Path) -> dict[str, str]:
    quarantine.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_OBJECT_DIRECTORY"] = str(quarantine)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(common_dir(repo) / "objects")
    return env


def admission_path(repo: Path) -> Path:
    return common_dir(repo) / "aider" / "transactions" / "admission.json"


def acquire_admissions(plan: dict[str, Any], transaction_id: str, expires: int, fence: int) -> tuple[bool, list[Path]]:
    acquired: list[Path] = []
    ordered = sorted(plan["repositories"], key=lambda item: str(common_dir(Path(item["path"]))))
    now = int(time.time() * 1000)
    for item in ordered:
        path = admission_path(Path(item["path"]))
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {"expires": now + 1}
            if existing.get("transaction_id") != transaction_id and int(existing.get("expires", 0)) > now:
                for owned in acquired:
                    owned.unlink(missing_ok=True)
                return False, []
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, {"schema_version": 3, "transaction_id": transaction_id, "expires": expires, "fence": fence})
        acquired.append(path)
    return True, acquired


def renew_admissions(ledger: dict[str, Any]) -> None:
    for item in ledger["repositories"]:
        atomic_json(admission_path(Path(item["repo"])), {"schema_version": 3, "transaction_id": ledger["transaction_id"], "expires": ledger["expires"], "fence": ledger["fence"]})


def release_admissions(ledger: dict[str, Any]) -> None:
    for item in ledger["repositories"]:
        path = admission_path(Path(item["repo"]))
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("transaction_id") == ledger["transaction_id"]:
                path.unlink()
        except (OSError, json.JSONDecodeError):
            pass
    ledger["cleanup"]["admissions_released"] = True


def run_task(ledger: dict[str, Any], task: dict[str, Any], state: Path, repositories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    repo_record = repositories[task["repository_id"]]
    repo = Path(repo_record["repo"])
    worktree = state / "worktrees" / task["repository_id"] / task["id"]
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), repo_record["base_revision"])
    env = dict(os.environ)
    env.update({
        "AIDER_PLAN_ID": ledger["plan_id"], "AIDER_SUBTASK_ID": task["id"], "AIDER_REPOSITORY_ID": task["repository_id"],
        "AIDER_BASE_REPO": str(repo), "AIDER_WORKTREE": str(worktree),
    })
    worker = call(task["worker"]["argv"], worktree, env=env)
    if worker.returncode:
        raise RuntimeError("worker failed")
    tested = call(task["test"]["argv"], worktree, env=env)
    if tested.returncode:
        raise RuntimeError("local test failed")
    qenv = quarantine_env(repo, Path(repo_record["quarantine_path"]))
    git(worktree, "add", "-A", env=qenv)
    git(worktree, "commit", "-q", "-m", f"public task {task['id']}", env=qenv)
    commit = git(worktree, "rev-parse", "HEAD", env=qenv).strip()
    snapshot = store_object(state, {"task": task["id"], "repository": task["repository_id"], "commit": commit})
    return {
        "id": task["id"], "repository_id": task["repository_id"], "state": "committed", "depends_on": task["depends_on"],
        "branch": None, "worktree": str(worktree),
        "snapshot": {"digest": snapshot, "entry_count": len(task["allowed_paths"]), "total_bytes": 0},
        "commit": commit, "test": completed_test(task["test"]["argv"], tested), "reason": None,
    }


def candidate_for_repository(ledger: dict[str, Any], record: dict[str, Any], task_results: list[dict[str, Any]], candidates: dict[str, str], state: Path) -> None:
    repo = Path(record["repo"])
    result = next(item for item in task_results if item["repository_id"] == record["id"])
    worktree = Path(result["worktree"])
    qenv = quarantine_env(repo, Path(record["quarantine_path"]))
    for link in ledger["plan"]["publication"]["links"]:
        if link["parent_repository_id"] == record["id"]:
            git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{candidates[link['child_repository_id']]},{link['path']}", env=qenv)
    integration_spec = next(item for item in ledger["plan"]["integration_tests"] if item["repository_id"] == record["id"])
    env = dict(os.environ)
    env.update({"AIDER_PLAN_ID": ledger["plan_id"], "AIDER_SUBTASK_ID": "integration", "AIDER_REPOSITORY_ID": record["id"], "AIDER_BASE_REPO": str(repo), "AIDER_WORKTREE": str(worktree)})
    tested = call(integration_spec["command"]["argv"], worktree, env=env)
    if tested.returncode:
        raise RuntimeError("integration failed")
    ledger["integration_tests"].append({"repository_id": record["id"], "result": completed_test(integration_spec["command"]["argv"], tested)})
    tree = git(worktree, "write-tree", env=qenv).strip()
    message = next(item["message"] for item in ledger["plan"]["publication"]["commit_messages"] if item["repository_id"] == record["id"])
    candidate = git(repo, "commit-tree", tree, "-p", record["base_revision"], "-m", message, env=qenv).strip()
    candidates[record["id"]] = candidate
    closure_files = sorted(path.relative_to(Path(record["quarantine_path"])).as_posix() for path in Path(record["quarantine_path"]).rglob("*") if path.is_file())
    quarantine_digest = store_object(state, {"repository": record["id"], "files": closure_files, "candidate": candidate})
    links = [link for link in ledger["plan"]["publication"]["links"] if link["parent_repository_id"] == record["id"] or link["child_repository_id"] == record["id"]]
    prepare_digest = store_object(state, {"repository": record["id"], "identity": record["identity"], "base": record["base_revision"], "candidate": candidate, "tree": tree, "links": links, "fence": ledger["fence"]})
    record.update({"state": "prepared", "candidate_commit": candidate, "tree_oid": tree, "prepare_digest": prepare_digest, "quarantine_digest": quarantine_digest})


def prepare_run(ledger: dict[str, Any], state: Path) -> None:
    repositories = {item["id"]: item for item in ledger["repositories"]}
    task_results = [run_task(ledger, task, state, repositories) for task in ledger["plan"]["subtasks"]]
    ledger["subtasks"] = task_results
    candidates: dict[str, str] = {}
    for identifier in ledger["plan"]["publication"]["participant_order"]:
        candidate_for_repository(ledger, repositories[identifier], task_results, candidates, state)
    targets = []
    for item in ledger["plan"]["publication"]["targets"]:
        repo = Path(repositories[item["repository_id"]]["repo"])
        targets.append({
            "repository_id": item["repository_id"], "ref": item["ref"], "expected_oid": item["expected_oid"],
            "observed_oid": oid(repo, item["ref"]), "final_oid": None, "state": "prepared",
        })
    decision_digest = store_object(state, {"participants": ledger["plan"]["publication"]["participant_order"], "candidates": candidates, "targets": targets, "fence": ledger["fence"]})
    ledger["decision"] = {"state": "prepared", "digest": decision_digest, "participant_order": ledger["plan"]["publication"]["participant_order"], "completed_participants": []}
    ledger["publication"] = {"state": "prepared", "targets": targets}
    ledger["state"], ledger["reason"] = "prepared", None


def promote(record: dict[str, Any]) -> None:
    source = Path(record["quarantine_path"])
    destination = common_dir(Path(record["repo"])) / "objects"
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
    record["objects_promoted"] = True
    record["state"] = "promoted"


def publish_participant(ledger: dict[str, Any], record: dict[str, Any]) -> None:
    repo = Path(record["repo"])
    candidate = record["candidate_commit"]
    participant_targets = [item for item in ledger["publication"]["targets"] if item["repository_id"] == record["id"]]
    for item in participant_targets:
        observed = oid(repo, item["ref"])
        if observed == candidate:
            item.update({"observed_oid": candidate, "final_oid": candidate, "state": "committed"})
            continue
        if observed != item["expected_oid"]:
            item.update({"observed_oid": observed, "final_oid": observed, "state": "obstructed"})
            ledger["state"], ledger["reason"] = "blocked", "decision_obstructed"
            ledger["decision"]["state"] = "obstructed"
            ledger["publication"]["state"] = "obstructed"
            return
    promote(record)
    lines = ["start"]
    for item in participant_targets:
        if item["state"] == "committed":
            continue
        lines.append(f"update {item['ref']} {candidate} {item['expected_oid'] or '0' * 40}")
    if len(lines) > 1:
        lines.extend(["prepare", "commit"])
        git(repo, "update-ref", "--stdin", input_text="\n".join(lines) + "\n")
    for item in participant_targets:
        item.update({"observed_oid": candidate, "final_oid": candidate, "state": "committed"})
    record["state"] = "committed"
    if record["id"] not in ledger["decision"]["completed_participants"]:
        ledger["decision"]["completed_participants"].append(record["id"])


def cleanup(ledger: dict[str, Any]) -> None:
    for item in ledger["subtasks"]:
        repo = next(Path(record["repo"]) for record in ledger["repositories"] if record["id"] == item["repository_id"])
        path = item.get("worktree")
        if path:
            call(["git", "worktree", "remove", "--force", path], repo)
        item["worktree"], item["state"] = None, "cleaned"
    ledger["cleanup"] = {"state": "complete", "remaining_worktrees": [], "remaining_branches": [], "admissions_released": False}
    release_admissions(ledger)


def roll_forward(ledger: dict[str, Any], ledger_path: Path, crash: dict[str, Any] | None = None) -> None:
    if ledger["decision"]["state"] == "prepared":
        ledger["decision"]["state"] = "commit"
        ledger["publication"]["state"] = "decided"
        ledger["state"] = "decided"
        save(ledger_path, ledger)
    occurrence = 0
    records = {item["id"]: item for item in ledger["repositories"]}
    for identifier in ledger["decision"]["participant_order"]:
        if identifier in ledger["decision"]["completed_participants"]:
            continue
        publish_participant(ledger, records[identifier])
        if ledger["reason"] == "decision_obstructed":
            save(ledger_path, ledger)
            return
        save(ledger_path, ledger)
        occurrence += 1
        if crash and crash["after"] == "participant_refs_committed" and crash["occurrence"] == occurrence:
            os.kill(os.getpid(), signal.SIGKILL)
    for identifier in ledger["decision"]["participant_order"]:
        record = records[identifier]
        repo = Path(record["repo"])
        git(repo, "reset", "--hard", "-q", record["candidate_commit"])
    cleanup(ledger)
    ledger["decision"]["state"] = "complete"
    ledger["publication"]["state"] = "committed"
    ledger["state"], ledger["reason"] = "committed", None
    save(ledger_path, ledger)


def verify_owner(ledger: dict[str, Any], request: dict[str, Any]) -> bool:
    owner = request.get("coordinator") or {}
    return owner.get("id") == ledger["owner_id"] and owner.get("token") == ledger["token"] and owner.get("fence") == ledger["fence"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    request_value = json.loads(Path(args.request).read_text(encoding="utf-8"))
    response_path = Path(args.response)
    repo, state = Path(request_value["repo"]), Path(request_value["state_dir"])
    ledger_path = state / "ledger.json"
    operation = request_value["operation"]

    if operation == "create":
        plan = request_value["plan"]
        identities = [str(common_dir(Path(item["path"]))) for item in plan["repositories"]]
        if len(identities) != len(set(identities)):
            return emit(response_path, rejected_response(request_value, "duplicate_repository_identity"))
        now = int(time.time() * 1000)
        expires = now + request_value["lease_seconds"] * 1000
        transaction_id = uuid.uuid4().hex
        admitted, _paths = acquire_admissions(plan, transaction_id, expires, 1)
        if not admitted:
            return emit(response_path, rejected_response(request_value, "repository_admission_busy"))
        if state.exists():
            for item in plan["repositories"]:
                admission_path(Path(item["path"])).unlink(missing_ok=True)
            return emit(response_path, rejected_response(request_value, "state_not_empty"))
        state.mkdir(parents=True)
        repositories = []
        for item in plan["repositories"]:
            repositories.append({
                "id": item["id"], "repo": item["path"], "identity": str(common_dir(Path(item["path"]))),
                "base_revision": item["base_revision"], "base_snapshot": None, "state": "pending",
                "candidate_commit": None, "tree_oid": None, "prepare_digest": None, "quarantine_digest": None,
                "objects_promoted": False, "quarantine_path": str(state / "quarantine" / item["id"] / "objects"),
            })
        ledger = {
            "schema_version": 3, "plan_id": request_value["plan_id"], "transaction_id": transaction_id,
            "repo": str(repo), "state_dir": str(state), "plan": plan, "state": "created", "reason": None,
            "owner_id": request_value["coordinator"]["id"], "token": secrets.token_urlsafe(24), "fence": 1,
            "expires": expires, "generation": 0, "event_count": 0, "repositories": repositories,
            "subtasks": [{
                "id": item["id"], "repository_id": item["repository_id"], "state": "pending", "depends_on": item["depends_on"],
                "branch": None, "worktree": None, "snapshot": None, "commit": None, "test": empty_test(item["test"]["argv"]), "reason": None,
            } for item in plan["subtasks"]],
            "integration_tests": [{"repository_id": item["repository_id"], "result": empty_test(item["command"]["argv"])} for item in plan["integration_tests"]],
            "decision": {"state": "none", "digest": None, "participant_order": plan["publication"]["participant_order"], "completed_participants": []},
            "publication": {"state": "pending", "targets": [{
                "repository_id": item["repository_id"], "ref": item["ref"], "expected_oid": item["expected_oid"],
                "observed_oid": oid(Path(next(record["path"] for record in plan["repositories"] if record["id"] == item["repository_id"])), item["ref"]),
                "final_oid": None, "state": "pending",
            } for item in plan["publication"]["targets"]]},
            "cleanup": {"state": "pending", "remaining_worktrees": [], "remaining_branches": [], "admissions_released": False},
        }
        save(ledger_path, ledger)
        return emit(response_path, response(ledger, operation))

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if operation == "status":
        return emit(response_path, response(ledger, operation))
    if operation == "recover":
        owner = request_value["coordinator"]
        if owner["token"] is None:
            if int(time.time() * 1000) < ledger["expires"] or owner["fence"] != ledger["fence"]:
                return emit(response_path, response(ledger, operation, state="blocked", reason="lease_active_or_stale_fence"))
            ledger["owner_id"], ledger["token"], ledger["fence"] = owner["id"], secrets.token_urlsafe(24), ledger["fence"] + 1
        elif not verify_owner(ledger, request_value):
            return emit(response_path, response(ledger, operation, state="blocked", reason="stale_fence"))
        ledger["expires"] = int(time.time() * 1000) + request_value["lease_seconds"] * 1000
        renew_admissions(ledger)
        save(ledger_path, ledger)
        if ledger["decision"]["state"] in {"prepared", "commit"}:
            roll_forward(ledger, ledger_path)
        return emit(response_path, response(ledger, operation))
    if not verify_owner(ledger, request_value):
        return emit(response_path, response(ledger, operation, state="blocked", reason="stale_fence"))
    if operation == "run":
        if ledger["state"] == "committed":
            return emit(response_path, response(ledger, operation))
        if ledger["decision"]["state"] in {"prepared", "commit", "obstructed"}:
            # A durable prepared decision must be recovered, not rebuilt by a
            # second worker execution after a lost response or crash.
            return emit(response_path, response(ledger, operation, state="blocked", reason="durable_decision_requires_recover"))
        ledger["expires"] = int(time.time() * 1000) + request_value["lease_seconds"] * 1000
        renew_admissions(ledger)
        prepare_run(ledger, state)
        save(ledger_path, ledger)
        crash = request_value.get("crash")
        if crash and crash["after"] == "federation_prepared":
            if crash["mode"] == "sigkill":
                os.kill(os.getpid(), signal.SIGKILL)
            return emit(response_path, response(ledger, operation), 75)
        roll_forward(ledger, ledger_path, crash)
        return emit(response_path, response(ledger, operation))
    if operation == "rollback":
        if ledger["state"] == "committed":
            return emit(response_path, response(ledger, operation, state="blocked", reason="rollback_requires_uncommitted_decision"))
        for target in ledger["publication"]["targets"]:
            repo_for_target = next(Path(item["repo"]) for item in ledger["repositories"] if item["id"] == target["repository_id"])
            observed = oid(repo_for_target, target["ref"])
            if observed != target["expected_oid"]:
                return emit(response_path, response(ledger, operation, state="blocked", reason="external_ref_drift"))
        cleanup(ledger)
        ledger["decision"]["state"] = "rolled_back"
        ledger["publication"]["state"] = "rolled_back"
        ledger["state"], ledger["reason"] = "rolled_back", None
        save(ledger_path, ledger)
        return emit(response_path, response(ledger, operation))
    return emit(response_path, response(ledger, operation, state="blocked", reason="unsupported_public_fixture_operation"))


if __name__ == "__main__":
    raise SystemExit(main())
