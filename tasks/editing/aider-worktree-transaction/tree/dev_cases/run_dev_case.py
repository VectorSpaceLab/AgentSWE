#!/usr/bin/env python3
"""Run the two self-contained public schema-v3 repository-set cases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "dev_cases" / "fixture_worker.py"
SCHEMAS = ROOT / "input" / "schemas"
REQUEST_VALIDATOR = Draft202012Validator(json.loads((SCHEMAS / "request.schema.json").read_text()))
RESPONSE_VALIDATOR = Draft202012Validator(json.loads((SCHEMAS / "response.schema.json").read_text()))
ENTRY_MODULE = "aider.worktree_plan_adapter"
FORBIDDEN_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GATEWAY_API_KEY", "SERPER_TOKEN")


class HarnessError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, input=input_text, text=True, capture_output=True, timeout=timeout, check=False)


def git(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    done = run(["git", *args], cwd=repo, env=env)
    if check and done.returncode:
        raise HarnessError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_repo(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    origin, seed, checkout = root / "origin.git", root / "seed", root / "checkout"
    root.mkdir(parents=True)
    run(["git", "init", "--bare", "-q", str(origin)], cwd=root)
    run(["git", "init", "-q", "-b", "main", str(seed)], cwd=root)
    git(seed, "config", "user.name", "Public Repository-Set Fixture")
    git(seed, "config", "user.email", "public-repository-set@example.invalid")
    for name, content in files.items():
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "fixture base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    cloned = run(["git", "clone", "-q", str(origin), str(checkout)], cwd=root)
    if cloned.returncode:
        raise HarnessError(cloned.stderr)
    git(checkout, "config", "user.name", "Public Repository-Set Fixture")
    git(checkout, "config", "user.email", "public-repository-set@example.invalid")
    return checkout, git(checkout, "rev-parse", "HEAD").strip()


def make_repository_set(root: Path, manifest: dict[str, Any]) -> tuple[dict[str, Path], dict[str, str]]:
    config = manifest["repositories"]
    component, component_base = make_repo(root / "component-source", config["component"]["files"])
    superproject, _initial_root = make_repo(root / "root-source", config["root"]["files"])
    link = config["gitlink_path"]
    added = run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(component), link], cwd=superproject)
    if added.returncode:
        raise HarnessError(added.stderr)
    embedded = superproject / link
    git(embedded, "config", "user.name", "Public Repository-Set Fixture")
    git(embedded, "config", "user.email", "public-repository-set@example.invalid")
    git(superproject, "add", ".gitmodules", link)
    git(superproject, "commit", "-q", "-m", "register component")
    root_base = git(superproject, "rev-parse", "HEAD").strip()
    root_id, component_id = config["root"]["id"], config["component"]["id"]
    repos = {root_id: superproject, component_id: embedded}
    bases = {root_id: root_base, component_id: component_base}
    for identifier, repo in repos.items():
        git(repo, "update-ref", "refs/heads/release", bases[identifier])
    return repos, bases


def refs(repo: Path) -> dict[str, str]:
    output = git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)")
    return dict(line.split("\0", 1) for line in output.splitlines() if "\0" in line)


def oid(repo: Path, revision: str) -> str | None:
    value = git(repo, "rev-parse", "-q", "--verify", revision, check=False).strip()
    return value or None


def common_dir(repo: Path) -> Path:
    value = Path(git(repo, "rev-parse", "--git-common-dir").strip())
    return value if value.is_absolute() else (repo / value).resolve()


def object_visible(repo: Path, object_id: str | None, quarantine: Path | None = None) -> bool:
    if not object_id:
        return False
    env = None
    if quarantine is not None:
        env = dict(os.environ)
        env["GIT_OBJECT_DIRECTORY"] = str(quarantine)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(common_dir(repo) / "objects")
    done = run(["git", "cat-file", "-e", f"{object_id}^{{commit}}"], cwd=repo, env=env)
    return done.returncode == 0


def log_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except json.JSONDecodeError:
            pass
    return result


@dataclass
class Invocation:
    operation: str
    returncode: int
    response: dict[str, Any] | None
    errors: list[str]
    duration_seconds: float


class Adapter:
    def __init__(self, python: Path, source: Path, evidence: Path, command_log: Path):
        self.python, self.source, self.evidence, self.command_log = python, source, evidence, command_log
        self.counter = 0
        self.env = dict(os.environ)
        self.env.update({"PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "localhost,127.0.0.1,::1", "AIDER_COMMAND_LOG": str(command_log)})
        for key in FORBIDDEN_ENV:
            self.env.pop(key, None)

    def invoke(self, value: dict[str, Any], *, expected: tuple[int, ...] = (0,), response_required: bool = True) -> Invocation:
        request_errors = sorted(REQUEST_VALIDATOR.iter_errors(value), key=lambda item: item.json_path)
        if request_errors:
            raise HarnessError(f"public runner generated invalid request: {request_errors[0].message}")
        self.counter += 1
        operation = value["operation"]
        request_path = self.evidence / f"{self.counter:03d}-{operation}-request.json"
        response_path = self.evidence / f"{self.counter:03d}-{operation}-response.json"
        write_json(request_path, value)
        started = time.monotonic()
        done = run([str(self.python), "-m", ENTRY_MODULE, "--request", str(request_path), "--response", str(response_path)], cwd=Path(value["repo"]), env=self.env, timeout=120)
        duration = time.monotonic() - started
        errors: list[str] = []
        response = None
        if done.returncode not in expected:
            errors.append(f"unexpected exit {done.returncode}; expected {expected}")
        if response_path.is_file():
            try:
                parsed = json.loads(response_path.read_text(encoding="utf-8"))
                response = parsed if isinstance(parsed, dict) else None
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"response parse failed: {exc}")
        elif response_required:
            errors.append("response file missing")
        if response is not None:
            errors.extend(f"schema {item.json_path}: {item.message}" for item in sorted(RESPONSE_VALIDATOR.iter_errors(response), key=lambda item: item.json_path))
            if response.get("operation") != operation or response.get("plan_id") != value["plan_id"]:
                errors.append("response identity mismatch")
            try:
                if json.loads(done.stdout) != response:
                    errors.append("stdout differs from response")
            except json.JSONDecodeError:
                errors.append("stdout is not one JSON object")
        return Invocation(operation, done.returncode, response, errors, duration)


def coordinator(identifier: str, token: str | None = None, fence: int | None = None) -> dict[str, Any]:
    return {"id": identifier, "token": token, "fence": fence}


def request(operation: str, repo: Path, state: Path, plan_id: str, *, plan: dict[str, Any] | None = None, max_workers: int | None = None, crash: dict[str, Any] | None = None, owner: dict[str, Any] | None = None, lease_seconds: int | None = None) -> dict[str, Any]:
    return {"schema_version": 3, "operation": operation, "repo": str(repo), "state_dir": str(state), "plan_id": plan_id, "plan": plan, "max_workers": max_workers, "crash": crash, "coordinator": owner, "lease_seconds": lease_seconds}


def active_owner(response: dict[str, Any], fallback: str) -> dict[str, Any]:
    value = response.get("coordination", {})
    return coordinator(str(value.get("owner_id") or fallback), value.get("lease_token"), value.get("fence"))


def fixture_command(python: Path, mode: str, spec: Path) -> dict[str, list[str]]:
    return {"argv": [str(python), str(WORKER), mode, str(spec)]}


def build_plan(manifest: dict[str, Any], repos: dict[str, Path], bases: dict[str, str], case_dir: Path, python: Path) -> dict[str, Any]:
    config = manifest["repositories"]
    root_id, component_id = config["root"]["id"], config["component"]["id"]
    repositories = [
        {"id": root_id, "path": str(repos[root_id]), "base_revision": bases[root_id], "base_state_policy": config["root"]["base_state_policy"], "role": "root"},
        {"id": component_id, "path": str(repos[component_id]), "base_revision": bases[component_id], "base_state_policy": config["component"]["base_state_policy"], "role": "component"},
    ]
    tasks = [{
        "id": item["id"], "repository_id": item["repository_id"], "depends_on": item["depends_on"],
        "allowed_paths": item["allowed_paths"],
        "worker": fixture_command(python, "worker", case_dir / "assets" / item["worker"]),
        "test": fixture_command(python, "test", case_dir / "assets" / item["test"]),
    } for item in manifest["subtasks"]]
    integration_tests = [{"repository_id": item["repository_id"], "command": fixture_command(python, "test", case_dir / "assets" / item["spec"])} for item in manifest["integration_tests"]]
    targets = []
    for identifier in manifest["participant_order"]:
        targets.extend([
            {"repository_id": identifier, "ref": "refs/heads/main", "expected_oid": bases[identifier]},
            {"repository_id": identifier, "ref": "refs/heads/release", "expected_oid": bases[identifier]},
        ])
    return {
        "repositories": repositories,
        "integration_order": [item["id"] for item in manifest["subtasks"]],
        "subtasks": tasks,
        "integration_tests": integration_tests,
        "publication": {
            "targets": targets,
            "participant_order": manifest["participant_order"],
            "links": [{"parent_repository_id": root_id, "path": config["gitlink_path"], "child_repository_id": component_id}],
            "commit_messages": [{"repository_id": identifier, "message": f"public repository-set {identifier}"} for identifier in manifest["participant_order"]],
            "on_ref_drift": "abort", "after_decision": "roll_forward",
        },
    }


def standalone_plan(repo: Path, base: str, identifier: str, task_id: str, python: Path, worker: Path, test: Path) -> dict[str, Any]:
    return {
        "repositories": [{"id": identifier, "path": str(repo), "base_revision": base, "base_state_policy": "require_clean", "role": "root"}],
        "integration_order": [task_id],
        "subtasks": [{"id": task_id, "repository_id": identifier, "depends_on": [], "allowed_paths": ["engine.txt"], "worker": fixture_command(python, "worker", worker), "test": fixture_command(python, "test", test)}],
        "integration_tests": [{"repository_id": identifier, "command": fixture_command(python, "test", test)}],
        "publication": {
            "targets": [{"repository_id": identifier, "ref": "refs/heads/main", "expected_oid": base}],
            "participant_order": [identifier], "links": [],
            "commit_messages": [{"repository_id": identifier, "message": "public disjoint"}],
            "on_ref_drift": "abort", "after_decision": "roll_forward"
        }
    }


def repository_records(response: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {item.get("id"): item for item in (response or {}).get("repositories", []) if isinstance(item, dict)}


def digest_object_ok(state: Path, digest: str | None) -> bool:
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return False
    expected = digest.split(":", 1)[1]
    path = state / "objects" / "sha256" / expected
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected


def refs_equal_candidates(repos: dict[str, Path], response: dict[str, Any] | None) -> bool:
    records = repository_records(response)
    for identifier, repo in repos.items():
        candidate = records.get(identifier, {}).get("candidate_commit")
        if not candidate or refs(repo).get("refs/heads/main") != candidate or refs(repo).get("refs/heads/release") != candidate:
            return False
    return True


def gitlink_oid(root: Path, revision: str, path: str) -> str | None:
    line = git(root, "ls-tree", revision, "--", path, check=False).strip()
    if not line:
        return None
    return line.split()[2]


def invocation_summary(value: Invocation) -> dict[str, Any]:
    return {"operation": value.operation, "exit_code": value.returncode, "state": value.response and value.response.get("state"), "errors": value.errors, "duration_seconds": round(value.duration_seconds, 3)}


def public_case_one(adapter: Adapter, repos: dict[str, Path], bases: dict[str, str], state: Path, manifest: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    root_id, component_id = manifest["repositories"]["root"]["id"], manifest["repositories"]["component"]["id"]
    root = repos[root_id]
    created = adapter.invoke(request("create", root, state, manifest["plan_id"], plan=plan, owner=coordinator(manifest["coordinator_id"]), lease_seconds=manifest["lease_seconds"]))
    if not created.response:
        return {"valid": False, "create": invocation_summary(created)}
    owner = active_owner(created.response, manifest["coordinator_id"])
    interrupted = adapter.invoke(request("run", root, state, manifest["plan_id"], max_workers=manifest["max_workers"], crash=manifest["crash"], owner=owner, lease_seconds=manifest["lease_seconds"]), expected=(75,))
    prepared = adapter.invoke(request("status", root, state, manifest["plan_id"]))
    records = repository_records(prepared.response)
    prepared_ok = bool(prepared.response and prepared.response.get("state") == "prepared" and prepared.response.get("decision", {}).get("state") == "prepared")
    quarantine_only = prepared_ok
    receipt_ok = prepared_ok
    for identifier, repo in repos.items():
        record = records.get(identifier, {})
        candidate = record.get("candidate_commit")
        quarantine_only = quarantine_only and not object_visible(repo, candidate) and object_visible(repo, candidate, state / "quarantine" / identifier / "objects") and refs(repo).get("refs/heads/main") == bases[identifier] and refs(repo).get("refs/heads/release") == bases[identifier]
        receipt_ok = receipt_ok and digest_object_ok(state, record.get("prepare_digest")) and digest_object_ok(state, record.get("quarantine_digest"))
    recovered = adapter.invoke(request("recover", root, state, manifest["plan_id"], owner=owner, lease_seconds=manifest["lease_seconds"]))
    one = adapter.invoke(request("status", root, state, manifest["plan_id"]))
    two = adapter.invoke(request("status", root, state, manifest["plan_id"]))
    final = recovered.response
    final_records = repository_records(final)
    component_candidate = final_records.get(component_id, {}).get("candidate_commit")
    root_candidate = final_records.get(root_id, {}).get("candidate_commit")
    linked = bool(component_candidate and root_candidate and gitlink_oid(root, root_candidate, manifest["repositories"]["gitlink_path"]) == component_candidate)
    parents = all(oid(repos[identifier], f"{final_records.get(identifier, {}).get('candidate_commit')}^") == bases[identifier] for identifier in repos)
    checkouts = (root / "app.txt").read_text() == "app-public\n" and (repos[component_id] / "core.txt").read_text() == "core-public\n"
    events = [item for item in log_events(adapter.command_log) if item.get("plan_id") == manifest["plan_id"]]
    workers = sum(item.get("kind") == "worker_start" for item in events)
    tests = sum(item.get("kind") == "test" for item in events)
    stable = bool(one.response and two.response and one.response.get("ledger", {}).get("digest") == two.response.get("ledger", {}).get("digest"))
    committed = bool(final and final.get("state") == "committed" and final.get("decision", {}).get("state") == "complete" and refs_equal_candidates(repos, final))
    clean = all(git(repo, "status", "--porcelain") == "" for repo in repos.values())
    valid = not any(item.errors for item in (created, interrupted, prepared, recovered, one, two)) and interrupted.returncode == 75 and quarantine_only and receipt_ok and committed and linked and parents and checkouts and workers == 2 and tests == 4 and clean and stable and final.get("cleanup", {}).get("admissions_released") is True
    return {"valid": valid, "create": invocation_summary(created), "prepare_exit": invocation_summary(interrupted), "prepared": invocation_summary(prepared), "recover": invocation_summary(recovered), "quarantine_only": quarantine_only, "prepare_receipts_valid": receipt_ok, "repository_set_committed": committed, "gitlink_bound": linked, "candidate_parents_valid": parents, "checkouts_synced": checkouts, "workers": workers, "tests": tests, "stable_status": stable}


def public_case_two(adapter: Adapter, repos: dict[str, Path], bases: dict[str, str], state: Path, manifest: dict[str, Any], plan: dict[str, Any], case_dir: Path, python: Path, output: Path) -> dict[str, Any]:
    root_id, component_id = manifest["repositories"]["root"]["id"], manifest["repositories"]["component"]["id"]
    root = repos[root_id]
    created = adapter.invoke(request("create", root, state, manifest["plan_id"], plan=plan, owner=coordinator(manifest["coordinator_id"]), lease_seconds=manifest["lease_seconds"]))
    if not created.response:
        return {"valid": False, "create": invocation_summary(created)}
    owner = active_owner(created.response, manifest["coordinator_id"])

    overlap_state = output / "overlap-state"
    overlap = adapter.invoke(request("create", root, overlap_state, "public-overlap", plan=plan, owner=coordinator("public-overlap-owner"), lease_seconds=5))
    overlap_blocked = bool(overlap.response and overlap.response.get("state") in {"blocked", "failed"} and not overlap_state.exists())

    killed = adapter.invoke(request("run", root, state, manifest["plan_id"], max_workers=manifest["max_workers"], crash=manifest["crash"], owner=owner, lease_seconds=manifest["lease_seconds"]), expected=(-signal.SIGKILL,), response_required=False)
    status = adapter.invoke(request("status", root, state, manifest["plan_id"]))
    records = repository_records(status.response)
    component_candidate = records.get(component_id, {}).get("candidate_commit")
    root_candidate = records.get(root_id, {}).get("candidate_commit")
    partial = bool(status.response and status.response.get("decision", {}).get("state") == "commit" and component_id in status.response.get("decision", {}).get("completed_participants", []) and refs(repos[component_id]).get("refs/heads/main") == component_candidate and refs(root).get("refs/heads/main") == bases[root_id] and object_visible(repos[component_id], component_candidate) and not object_visible(root, root_candidate))
    nonblocking = status.duration_seconds < 1.0

    disjoint_repo, disjoint_base = make_repo(output / "disjoint", {"engine.txt": "engine-base\n"})
    disjoint_plan = standalone_plan(disjoint_repo, disjoint_base, "disjoint", "disjoint-edit", python, case_dir / "assets" / "worker-engine.json", case_dir / "assets" / "test-engine.json")
    disjoint_state = output / "disjoint-state"
    disjoint_created = adapter.invoke(request("create", disjoint_repo, disjoint_state, "public-disjoint", plan=disjoint_plan, owner=coordinator("public-disjoint-owner"), lease_seconds=5))
    disjoint_owner = active_owner(disjoint_created.response or {}, "public-disjoint-owner")
    disjoint_run = adapter.invoke(request("run", disjoint_repo, disjoint_state, "public-disjoint", max_workers=1, owner=disjoint_owner, lease_seconds=5))
    disjoint_ok = bool(disjoint_created.response and disjoint_created.response.get("state") == "created" and disjoint_run.response and disjoint_run.response.get("state") == "committed")

    time.sleep(manifest["lease_seconds"] + 0.25)
    recovered = adapter.invoke(request("recover", root, state, manifest["plan_id"], owner=coordinator(manifest["takeover_id"], None, owner["fence"]), lease_seconds=manifest["lease_seconds"]))
    current = active_owner(recovered.response or {}, manifest["takeover_id"])
    before_stale = {identifier: refs(repo) for identifier, repo in repos.items()}
    stale = adapter.invoke(request("run", root, state, manifest["plan_id"], max_workers=2, owner=owner, lease_seconds=manifest["lease_seconds"]))
    stale_blocked = bool(stale.response and stale.response.get("state") in {"blocked", "failed"} and before_stale == {identifier: refs(repo) for identifier, repo in repos.items()})
    one = adapter.invoke(request("status", root, state, manifest["plan_id"]))
    two = adapter.invoke(request("status", root, state, manifest["plan_id"]))
    stable = bool(one.response and two.response and one.response.get("ledger", {}).get("digest") == two.response.get("ledger", {}).get("digest"))
    final_records = repository_records(recovered.response)
    linked = gitlink_oid(root, final_records.get(root_id, {}).get("candidate_commit", ""), manifest["repositories"]["gitlink_path"]) == final_records.get(component_id, {}).get("candidate_commit")
    committed = bool(recovered.response and recovered.response.get("state") == "committed" and refs_equal_candidates(repos, recovered.response) and current.get("fence") == owner["fence"] + 1)
    events = [item for item in log_events(adapter.command_log) if item.get("plan_id") == manifest["plan_id"]]
    workers = sum(item.get("kind") == "worker_start" for item in events)
    tests = sum(item.get("kind") == "test" for item in events)
    valid = not any(item.errors for item in (created, overlap, killed, status, disjoint_created, disjoint_run, recovered, stale, one, two)) and killed.returncode == -signal.SIGKILL and overlap_blocked and partial and nonblocking and disjoint_ok and committed and linked and stale_blocked and workers == 2 and tests == 4 and stable
    return {"valid": valid, "create": invocation_summary(created), "overlap": invocation_summary(overlap), "hard_crash": invocation_summary(killed), "partial_status": invocation_summary(status), "overlap_blocked": overlap_blocked, "partial_prefix_valid": partial, "status_nonblocking": nonblocking, "disjoint_committed": disjoint_ok, "recover": invocation_summary(recovered), "repository_set_committed": committed, "gitlink_bound": linked, "stale_fenced": stale_blocked, "workers": workers, "tests": tests, "stable_status": stable}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    case_dir, python, source, output = args.case_dir.resolve(), args.python.resolve(), args.source.resolve(), args.output_dir.resolve()
    protected = (case_dir, source, python.parent)
    if any(output == path or output in path.parents for path in protected):
        parser.error("output directory must not equal or contain a protected input")
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be absent or empty")
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((case_dir / "assets" / "manifest.json").read_text(encoding="utf-8"))
    repos, bases = make_repository_set(output / "repositories", manifest)
    root_id = manifest["repositories"]["root"]["id"]
    command_log = output / "command-log.jsonl"
    adapter = Adapter(python, source, output / "evidence", command_log)
    value = build_plan(manifest, repos, bases, case_dir, python)
    state = output / "state"
    if manifest["scenario"] == "quarantined-repository-set-publish":
        result = public_case_one(adapter, repos, bases, state, manifest, value)
    elif manifest["scenario"] == "partial-decision-admission-takeover":
        result = public_case_two(adapter, repos, bases, state, manifest, value, case_dir, python, output)
    else:
        raise HarnessError(f"unknown public scenario {manifest['scenario']!r}")
    result.update({"case": case_dir.name, "schema_version": 3, "command_log": str(command_log), "root_repo": str(repos[root_id])})
    write_json(output / "dev_result.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
