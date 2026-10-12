#!/usr/bin/env python3
"""Run one isolated schema-v3 repository-set scenario as a black-box test."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from common import (
    Adapter,
    CASE_IDS,
    HarnessError,
    active_owner,
    assertion,
    coordinator,
    filesystem_state,
    git,
    log_events,
    make_repo,
    plan,
    read_json,
    refs,
    request,
    run,
    scored,
    subtask,
    tracked_tree,
    worktrees,
    write_json,
)

HERE = Path(__file__).resolve().parent
WORKER = HERE / "fixture_worker.py"

ASSERTION_MANIFEST: dict[str, tuple[tuple[str, int], ...]] = {
    "test_001": (
        ("SET-ADMISSION-SCOPE", 15), ("SET-DAG-REAL-CONCURRENCY", 15),
        ("SET-PREDECISION-DEATH", 15), ("SET-QUARANTINE-AUTHORITY", 15),
        ("SET-TAKEOVER-GITLINKS", 20), ("SET-FENCE-EFFECTS-ONCE", 10),
        ("SET-CLEAN-NONBLOCKING-STATUS", 10),
    ),
    "test_002": (
        ("CLOSURE-PREPARED-ONLY", 15), ("CLOSURE-PUBLIC-RECEIPTS", 15),
        ("CLOSURE-REBUILD-QUARANTINES", 15), ("CLOSURE-PUBLISH-GITLINK", 15),
        ("CLOSURE-ENTRY-FIDELITY", 15), ("CLOSURE-FILTER-FIDELITY", 10),
        ("CLOSURE-EFFECTS-ONCE", 5), ("CLOSURE-CORRUPTION-CLOSED", 10),
    ),
    "test_003": (
        ("PREFIX-DURABLE-DECISION", 15), ("PREFIX-STATUS-PURE", 10),
        ("PREFIX-REHYDRATE-COMMITTED", 15), ("PREFIX-ROLL-FORWARD-SUFFIX", 20),
        ("PREFIX-MODES-AND-GITLINKS", 15), ("PREFIX-HOOKS-FILTERS-ONCE", 15),
        ("PREFIX-CLEAN-STABLE", 10),
    ),
    "test_004": (
        ("CONFLICT-SET-NO-DECISION", 20), ("CONFLICT-ALL-REFS-UNCHANGED", 15),
        ("CONFLICT-VARIANTS-QUARANTINED", 15), ("CONFLICT-STABLE-EFFECTS", 10),
        ("CONFLICT-FOREIGN-LOCK", 10), ("CONFLICT-DISJOINT-COMMITS", 15),
        ("CONFLICT-ROLLBACK-OWNERSHIP", 15),
    ),
    "test_005": (
        ("REVERSE-SET-COMMIT", 15), ("REVERSE-DRIFT-ABORT-ALL", 20),
        ("REVERSE-DECISION-PREFIX", 15), ("REVERSE-TAKEOVER-SUFFIX", 15),
        ("REVERSE-EXACT-TWO-REPOSITORIES", 25), ("REVERSE-ONCE-STABLE", 10),
    ),
    "test_006": (
        ("GUARD-ALIAS-FOREIGN-ADMISSION", 15), ("GUARD-DECISION-CORRUPTION", 15),
        ("GUARD-POSTDECISION-OBSTRUCTION", 20), ("GUARD-WITHIN-PARTICIPANT-MIXTURE", 10),
        ("GUARD-STATUS-PURITY", 15), ("COMPAT-AIDER-VERSION", 10),
        ("COMPAT-GIT-SUBMODULE-WORKTREE", 15),
    ),
}


class Case:
    def __init__(self, case_id: str, python: Path, source: Path, root: Path):
        self.case_id, self.python, self.source, self.root = case_id, python, source, root
        self.specs, self.command_log = root / "fixture-specs", root / "command-log.jsonl"
        self.invocations: list[dict[str, Any]] = []
        self.adapter_counter = 0

    def adapter(self, name: str | None = None) -> Adapter:
        self.adapter_counter += 1
        return Adapter(self.python, self.source, self.root / "evidence" / (name or f"adapter-{self.adapter_counter:02d}"), self.command_log)

    def spec(self, name: str, value: dict[str, Any]) -> Path:
        path = self.specs / f"{name}.json"
        write_json(path, value)
        return path

    def command(self, mode: str, spec: Path) -> dict[str, list[str]]:
        return {"argv": [str(self.python), str(WORKER), mode, str(spec)]}

    def invoke(self, adapter: Adapter, value: dict[str, Any], expected: tuple[int, ...] | None = None, response_required: bool | None = None):
        hard = bool(value.get("crash") and value["crash"].get("mode") == "sigkill")
        cooperative = bool(value.get("crash") and value["crash"].get("mode") == "exit75")
        result = adapter.invoke(
            value,
            expected=expected or ((-signal.SIGKILL,) if hard else ((75,) if cooperative else (0,))),
            response_required=(not hard if response_required is None else response_required),
        )
        self.invocations.append({
            "operation": result.operation, "exit_code": result.returncode,
            "duration_seconds": round(result.duration_seconds, 3), "peak_pss_bytes": result.peak_pss_bytes,
            "memory_exceeded": result.memory_exceeded, "schema_errors": result.shape_errors,
        })
        return result


def failed(case: Case, evidence: str, passed_ids: set[str] | None = None) -> dict[str, Any]:
    passed_ids = passed_ids or set()
    values = [assertion(identifier, points, identifier in passed_ids, evidence) for identifier, points in ASSERTION_MANIFEST[case.case_id]]
    return scored(case.case_id, values, commands=case.invocations)


def owner_from(result: Any, fallback: str) -> dict[str, Any] | None:
    if result.response is None:
        return None
    value = active_owner(result.response, fallback)
    return value if isinstance(value.get("token"), str) and isinstance(value.get("fence"), int) else None


@dataclass
class RepoSet:
    root_id: str
    repos: dict[str, Path]
    bases: dict[str, str]
    links: list[dict[str, str]]
    nested_second: str | None = None

    @property
    def root(self) -> Path:
        return self.repos[self.root_id]


def add_submodule(parent: Path, source: Path, link: str, message: str) -> str:
    added = run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), link], cwd=parent)
    if added.returncode:
        raise HarnessError(added.stderr)
    git(parent, "add", ".gitmodules", link)
    git(parent, "commit", "-q", "-m", message)
    return git(parent, "rev-parse", "HEAD").strip()


def make_repo_set(
    root: Path,
    root_id: str,
    root_files: dict[str, str | bytes],
    components: dict[str, tuple[str, dict[str, str | bytes]]],
    *,
    nested_component: str | None = None,
) -> RepoSet:
    sources: dict[str, Path] = {}
    bases: dict[str, str] = {}
    nested_second = None
    for identifier, (_link, files) in components.items():
        _origin, source, base = make_repo(root / f"source-{identifier}", files)
        sources[identifier], bases[identifier] = source, base
    if nested_component is not None:
        _origin, nested_source, nested_first = make_repo(root / "nested-source", {"nested.txt": "nested-one\n"})
        parent = sources[nested_component]
        bases[nested_component] = add_submodule(parent, nested_source, "nested/lib", "register nested component")
        (nested_source / "nested.txt").write_text("nested-two\n")
        git(nested_source, "commit", "-q", "-am", "nested two")
        nested_second = git(nested_source, "rev-parse", "HEAD").strip()
        git(parent / "nested/lib", "checkout", "-q", nested_first)
    _origin, root_repo, _initial = make_repo(root / "source-root", root_files)
    links = []
    for identifier, (link, _files) in components.items():
        add_submodule(root_repo, sources[identifier], link, f"register {identifier}")
        links.append({"parent_repository_id": root_id, "path": link, "child_repository_id": identifier})
    bases[root_id] = git(root_repo, "rev-parse", "HEAD").strip()
    repos = {root_id: root_repo}
    for identifier, (link, _files) in components.items():
        embedded = root_repo / link
        git(embedded, "config", "user.name", "Evaluator Fixture")
        git(embedded, "config", "user.email", "fixture@example.invalid")
        repos[identifier] = embedded
    for identifier, repo in repos.items():
        git(repo, "update-ref", "refs/heads/release", bases[identifier])
    return RepoSet(root_id, repos, bases, links, nested_second)


def repository_descriptors(repo_set: RepoSet, snapshot: set[str] | None = None) -> list[dict[str, Any]]:
    snapshot = snapshot or set()
    return [{
        "id": identifier, "path": str(repo), "base_revision": repo_set.bases[identifier],
        "base_state_policy": "snapshot" if identifier in snapshot else "require_clean",
        "role": "root" if identifier == repo_set.root_id else "component",
    } for identifier, repo in repo_set.repos.items()]


def target(repository_id: str, ref: str, expected: str | None) -> dict[str, Any]:
    return {"repository_id": repository_id, "ref": ref, "expected_oid": expected}


def all_targets(repo_set: RepoSet, *, absent_root_audit: bool = False) -> list[dict[str, Any]]:
    values = []
    for identifier in repo_set.repos:
        values.extend([target(identifier, "refs/heads/main", repo_set.bases[identifier]), target(identifier, "refs/heads/release", repo_set.bases[identifier])])
    if absent_root_audit:
        values.append(target(repo_set.root_id, "refs/heads/audit", None))
    return values


def integration_tests(case: Case, specs: dict[str, Path]) -> list[dict[str, Any]]:
    return [{"repository_id": identifier, "command": case.command("test", path)} for identifier, path in specs.items()]


def repo_records(response: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {item.get("id"): item for item in (response or {}).get("repositories", []) if isinstance(item, dict)}


def candidate(response: dict[str, Any] | None, repository_id: str) -> str | None:
    value = repo_records(response).get(repository_id, {}).get("candidate_commit")
    return value if isinstance(value, str) and len(value) == 40 else None


def ref_oid(repo: Path, ref: str) -> str | None:
    value = git(repo, "rev-parse", "-q", "--verify", ref, check=False).strip()
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
    return run(["git", "cat-file", "-e", f"{object_id}^{{commit}}"], cwd=repo, env=env).returncode == 0


def object_path(state: Path, digest: str | None) -> Path | None:
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    return state / "objects" / "sha256" / digest.split(":", 1)[1]


def digest_ok(state: Path, digest: str | None) -> bool:
    path = object_path(state, digest)
    return bool(path and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest.split(":", 1)[1])


def gitlink_oid(repo: Path, revision: str | None, path: str) -> str | None:
    if not revision:
        return None
    line = git(repo, "ls-tree", revision, "--", path, check=False).strip()
    return line.split()[2] if line else None


def all_refs_at_candidates(repo_set: RepoSet, response: dict[str, Any] | None, *, audit: bool = False) -> bool:
    for identifier, repo in repo_set.repos.items():
        value = candidate(response, identifier)
        refs_expected = ["refs/heads/main", "refs/heads/release"] + (["refs/heads/audit"] if audit and identifier == repo_set.root_id else [])
        if not value or any(ref_oid(repo, ref) != value for ref in refs_expected):
            return False
    return True


def links_match(repo_set: RepoSet, response: dict[str, Any] | None) -> bool:
    root_candidate = candidate(response, repo_set.root_id)
    return all(gitlink_oid(repo_set.root, root_candidate, link["path"]) == candidate(response, link["child_repository_id"]) for link in repo_set.links)


def event_counts(case: Case, plan_id: str, task_ids: tuple[str, ...]) -> tuple[dict[str, int], int, list[dict[str, Any]]]:
    events = [item for item in log_events(case.command_log) if item.get("plan_id") == plan_id]
    workers = {identifier: sum(item.get("kind") == "worker_start" and item.get("subtask_id") == identifier for item in events) for identifier in task_ids}
    tests = sum(item.get("kind") == "test" for item in events)
    return workers, tests, events


def simple_specs(case: Case, prefix: str, path: str, content: str, delay: float = 0) -> tuple[Path, Path]:
    worker = case.spec(prefix + "-worker", {"delay_seconds": delay, "actions": [{"op": "write", "path": path, "content": content}]})
    test = case.spec(prefix + "-test", {"assertions": [{"op": "contains", "path": path, "content": content.strip()}]})
    return worker, test


def install_filter(repo: Path, log: Path | None = None) -> None:
    if log is None:
        clean, smudge = "sed s/WORKTREE/GITBLOB/g", "sed s/GITBLOB/WORKTREE/g"
    else:
        driver = log.with_suffix(".sh")
        driver.parent.mkdir(parents=True, exist_ok=True)
        driver.write_text(
            "#!/bin/sh\n" + f"printf '%s\\n' \"$1\" >> {shlex.quote(str(log))}\n" +
            "if [ \"$1\" = clean ]; then sed s/WORKTREE/GITBLOB/g; else sed s/GITBLOB/WORKTREE/g; fi\n"
        )
        driver.chmod(0o755)
        clean, smudge = f"{shlex.quote(str(driver))} clean", f"{shlex.quote(str(driver))} smudge"
    git(repo, "config", "filter.bench.clean", clean)
    git(repo, "config", "filter.bench.smudge", smudge)
    git(repo, "config", "filter.bench.required", "true")


def install_hooks(repo: Path, commit_log: Path, reference_log: Path | None = None) -> None:
    hooks = common_dir(repo) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    pre = hooks / "pre-commit"
    pre.write_text(f"#!/bin/sh\nprintf '%s\\n' \"${{AIDER_SUBTASK_ID:-ordinary}}\" >> {str(commit_log)!r}\n")
    pre.chmod(0o755)
    if reference_log is not None:
        reference = hooks / "reference-transaction"
        reference.write_text(f"#!/bin/sh\nphase=$1\nwhile read old new ref; do printf '%s %s\\n' \"$phase\" \"$ref\" >> {str(reference_log)!r}; done\n")
        reference.chmod(0o755)


def advance_ref(repo: Path, ref: str, parent: str, message: str) -> str:
    tree = git(repo, "rev-parse", f"{parent}^{{tree}}").strip()
    object_id = git(repo, "commit-tree", tree, "-p", parent, "-m", message).strip()
    git(repo, "update-ref", ref, object_id, parent)
    return object_id


def remove_loose_object(repo: Path, object_id: str | None) -> bool:
    if not object_id:
        return False
    path = common_dir(repo) / "objects" / object_id[:2] / object_id[2:]
    if path.is_file():
        path.unlink()
        return True
    return False


def state_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    for item in sorted(path.rglob("*")):
        digest.update(item.relative_to(path).as_posix().encode())
        if item.is_file() and not item.is_symlink():
            digest.update(item.read_bytes())
        elif item.is_symlink():
            digest.update(os.readlink(item).encode())
    return digest.hexdigest()


def exact_repo_state(repo: Path) -> dict[str, Any]:
    return {
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "head_ref": git(repo, "symbolic-ref", "-q", "HEAD", check=False).strip() or None,
        "index": git(repo, "ls-files", "--stage", "-z"),
        "status": git(repo, "status", "--porcelain=v2", "-z"),
        "filesystem": filesystem_state(repo), "refs": refs(repo), "worktrees": worktrees(repo),
    }


def case_001(case: Case) -> dict[str, Any]:
    started = time.monotonic()
    repo_set = make_repo_set(
        case.root / "set", "root", {"root-a.txt": "a\n", "root-b.txt": "b\n", "join.txt": "join\n"},
        {"alpha": ("modules/alpha", {"alpha.txt": "alpha\n"}), "beta": ("modules/beta", {"beta.txt": "beta\n"})},
    )
    specs = {
        "alpha": simple_specs(case, "set-alpha", "alpha.txt", "alpha-set\n", 0.7),
        "beta": simple_specs(case, "set-beta", "beta.txt", "beta-set\n", 0.7),
        "root-a": simple_specs(case, "set-root-a", "root-a.txt", "root-a-set\n", 0.7),
        "root-b": simple_specs(case, "set-root-b", "root-a.txt", "root-b-set\n", 0.1),
        "join": simple_specs(case, "set-join", "join.txt", "join-set\n"),
    }
    tasks = [
        subtask("alpha", "alpha", [], ["alpha.txt"], case.command("worker", specs["alpha"][0]), case.command("test", specs["alpha"][1])),
        subtask("beta", "beta", [], ["beta.txt"], case.command("worker", specs["beta"][0]), case.command("test", specs["beta"][1])),
        subtask("root-a", "root", [], ["root-a.txt"], case.command("worker", specs["root-a"][0]), case.command("test", specs["root-a"][1])),
        subtask("root-b", "root", [], ["root-a.txt"], case.command("worker", specs["root-b"][0]), case.command("test", specs["root-b"][1])),
        subtask("join", "root", ["alpha", "beta", "root-a", "root-b"], ["join.txt"], case.command("worker", specs["join"][0]), case.command("test", specs["join"][1])),
    ]
    integration = {identifier: case.spec(f"set-integration-{identifier}", {"assertions": [{"op": "exists", "path": "."}]}) for identifier in repo_set.repos}
    value = plan(repository_descriptors(repo_set), tasks, integration_tests(case, integration), integration_order=[item["id"] for item in tasks], targets=all_targets(repo_set), participant_order=["alpha", "beta", "root"], links=repo_set.links)
    state, adapter = case.root / "state", case.adapter("owner")
    created = case.invoke(adapter, request("create", repo_set.root, state, "set-dag", plan=value, owner=coordinator("owner-a"), lease_seconds=1))
    owner = owner_from(created, "owner-a")
    if owner is None:
        return failed(case, "repository-set create/admission unavailable")

    overlap_state = case.root / "overlap-state"
    overlap = case.invoke(case.adapter("overlap"), request("create", repo_set.root, overlap_state, "overlap", plan=value, owner=coordinator("overlap-owner"), lease_seconds=5))
    overlap_ok = bool(overlap.response and overlap.response.get("state") in {"blocked", "failed"} and not overlap_state.exists())
    _origin, disjoint_repo, disjoint_base = make_repo(case.root / "disjoint", {"only.txt": "only\n"})
    dw, dt = simple_specs(case, "disjoint", "only.txt", "disjoint\n")
    disjoint_value = plan(
        [{"id": "disjoint", "path": str(disjoint_repo), "base_revision": disjoint_base, "base_state_policy": "require_clean", "role": "root"}],
        [subtask("disjoint-task", "disjoint", [], ["only.txt"], case.command("worker", dw), case.command("test", dt))],
        [{"repository_id": "disjoint", "command": case.command("test", dt)}],
        targets=[target("disjoint", "refs/heads/main", disjoint_base)], participant_order=["disjoint"],
    )
    disjoint_state = case.root / "disjoint-state"
    disjoint = case.invoke(case.adapter("disjoint"), request("create", disjoint_repo, disjoint_state, "disjoint-plan", plan=disjoint_value, owner=coordinator("disjoint-owner"), lease_seconds=5))
    disjoint_ok = bool(disjoint.response and disjoint.response.get("state") == "created")

    killed = case.invoke(adapter, request("run", repo_set.root, state, "set-dag", max_workers=3, crash={"after": "participant_ref_prepared", "occurrence": 2, "mode": "sigkill"}, owner=owner, lease_seconds=1))
    prepared = case.invoke(case.adapter("prepared-status"), request("status", repo_set.root, state, "set-dag"))
    records = repo_records(prepared.response)
    refs_unchanged = all(ref_oid(repo, "refs/heads/main") == repo_set.bases[identifier] and ref_oid(repo, "refs/heads/release") == repo_set.bases[identifier] for identifier, repo in repo_set.repos.items())
    quarantine_only = bool(records) and all(not object_visible(repo_set.repos[identifier], record.get("candidate_commit")) and object_visible(repo_set.repos[identifier], record.get("candidate_commit"), state / "quarantine" / identifier / "objects") for identifier, record in records.items())
    receipts = bool(records) and all(digest_ok(state, record.get("prepare_digest")) and digest_ok(state, record.get("quarantine_digest")) for record in records.values())
    prepared_death = killed.returncode == -signal.SIGKILL and refs_unchanged and (prepared.response or {}).get("decision", {}).get("state") == "prepared"
    time.sleep(1.25)
    recovered = case.invoke(case.adapter("takeover"), request("recover", repo_set.root, state, "set-dag", owner=coordinator("owner-b", None, owner["fence"]), lease_seconds=1))
    current = owner_from(recovered, "owner-b")
    before_stale = {identifier: refs(repo) for identifier, repo in repo_set.repos.items()}
    stale = case.invoke(case.adapter("stale"), request("run", repo_set.root, state, "set-dag", max_workers=3, owner=owner, lease_seconds=1))
    stale_ok = bool(stale.response and stale.response.get("state") in {"blocked", "failed"} and before_stale == {identifier: refs(repo) for identifier, repo in repo_set.repos.items()})
    one = case.invoke(case.adapter("status-one"), request("status", repo_set.root, state, "set-dag"))
    two = case.invoke(case.adapter("status-two"), request("status", repo_set.root, state, "set-dag"))
    workers, tests, events = event_counts(case, "set-dag", ("alpha", "beta", "root-a", "root-b", "join"))
    starts = {item.get("subtask_id"): index for index, item in enumerate(events) if item.get("kind") == "worker_start"}
    ends = {item.get("subtask_id"): index for index, item in enumerate(events) if item.get("kind") == "worker_end"}
    active = read_json(case.command_log.with_suffix(".active.json")) if case.command_log.with_suffix(".active.json").exists() else {}
    scope_serial = starts.get("root-b", 0) > ends.get("root-a", 10**9) or starts.get("root-a", 0) > ends.get("root-b", 10**9)
    dependency = "join" in starts and all(starts["join"] > ends.get(name, 10**9) for name in ("alpha", "beta", "root-a", "root-b"))
    concurrency = active.get("peak") == 3 and scope_serial and dependency
    committed = bool(recovered.response and recovered.response.get("state") == "committed" and all_refs_at_candidates(repo_set, recovered.response) and links_match(repo_set, recovered.response) and current and current["fence"] == owner["fence"] + 1)
    stable = bool(one.response and two.response and one.response.get("ledger", {}).get("digest") == two.response.get("ledger", {}).get("digest") and one.duration_seconds < 1)
    clean = all(len(worktrees(repo)) == 1 and git(repo, "status", "--porcelain") == "" for repo in repo_set.repos.values())
    assertions = [
        assertion("SET-ADMISSION-SCOPE", 15, overlap_ok and disjoint_ok, f"overlap={overlap_ok} disjoint={disjoint_ok}"),
        assertion("SET-DAG-REAL-CONCURRENCY", 15, concurrency, f"peak={active.get('peak')} scope={scope_serial} dependency={dependency}"),
        assertion("SET-PREDECISION-DEATH", 15, prepared_death, f"exit={killed.returncode} refs_unchanged={refs_unchanged}"),
        assertion("SET-QUARANTINE-AUTHORITY", 15, quarantine_only and receipts, f"quarantine_only={quarantine_only} receipts={receipts}"),
        assertion("SET-TAKEOVER-GITLINKS", 20, committed, f"committed={committed}"),
        assertion("SET-FENCE-EFFECTS-ONCE", 10, stale_ok and all(count == 1 for count in workers.values()) and tests == 8, f"stale={stale_ok} workers={workers} tests={tests}"),
        assertion("SET-CLEAN-NONBLOCKING-STATUS", 10, clean and stable, f"clean={clean} stable={stable}"),
    ]
    duplicate = any(count > 1 for count in workers.values()) or tests > 8
    authority_leak = prepared_death and not quarantine_only
    cap = 10 if duplicate or authority_leak else None
    return scored(case.case_id, assertions, cap=cap, cap_reason="duplicate fenced effect or predecision object leak" if cap else None, commands=case.invocations, duration=time.monotonic() - started)


def shape_actions() -> list[dict[str, Any]]:
    return [
        {"op": "write", "path": "edit.txt", "content": "edited\n"},
        {"op": "rename", "path": "left.txt", "to": "cycle.tmp"},
        {"op": "rename", "path": "right.txt", "to": "left.txt"},
        {"op": "rename", "path": "cycle.tmp", "to": "right.txt"},
        {"op": "delete", "path": "delete.txt"}, {"op": "chmod", "path": "script.sh", "executable": True},
        {"op": "symlink", "path": "link", "target": "right.txt"},
        {"op": "write_base64", "path": "binary.bin", "content": "AP8QAGZpeHR1cmUtcHJpdmF0ZQ=="},
        {"op": "write", "path": "filtered.txt", "content": "WORKTREE changed\n"},
    ]


def setup_shape_set(case: Case, root: Path, prefix: str) -> tuple[RepoSet, dict[str, Any]]:
    repo_set = make_repo_set(
        root, "root", {"root.txt": "root\n"},
        {"component": ("vendor/component", {"edit.txt": "base\n", "left.txt": "LEFT\n", "right.txt": "RIGHT\n", "delete.txt": "delete\n", "script.sh": "#!/bin/sh\n", "filtered.txt": "WORKTREE\n", ".gitattributes": "filtered.txt filter=bench\n"})},
    )
    install_filter(repo_set.repos["component"])
    shape_worker = case.spec(prefix + "-shape-worker", {"actions": shape_actions()})
    shape_test = case.spec(prefix + "-shape-test", {"assertions": [{"op": "contains", "path": "edit.txt", "content": "edited"}, {"op": "executable", "path": "script.sh"}, {"op": "symlink", "path": "link", "target": "right.txt"}, {"op": "exists", "path": "binary.bin"}]})
    root_worker, root_test = simple_specs(case, prefix + "-root", "root.txt", "root-changed\n")
    tasks = [
        subtask("shape", "component", [], ["edit.txt", "left.txt", "right.txt", "cycle.tmp", "delete.txt", "script.sh", "link", "binary.bin", "filtered.txt"], case.command("worker", shape_worker), case.command("test", shape_test)),
        subtask("root-edit", "root", [], ["root.txt"], case.command("worker", root_worker), case.command("test", root_test)),
    ]
    integrations = {"component": shape_test, "root": root_test}
    value = plan(repository_descriptors(repo_set), tasks, integration_tests(case, integrations), targets=all_targets(repo_set), participant_order=["component", "root"], links=repo_set.links)
    return repo_set, value


def case_002(case: Case) -> dict[str, Any]:
    started = time.monotonic()
    repo_set, value = setup_shape_set(case, case.root / "normal", "closure")
    state, adapter = case.root / "normal-state", case.adapter("normal")
    created = case.invoke(adapter, request("create", repo_set.root, state, "closure", plan=value, owner=coordinator("owner-a"), lease_seconds=1))
    owner = owner_from(created, "owner-a")
    if owner is None:
        return failed(case, "quarantine prepare unavailable")
    killed = case.invoke(adapter, request("run", repo_set.root, state, "closure", max_workers=2, crash={"after": "federation_prepared", "occurrence": 1, "mode": "sigkill"}, owner=owner, lease_seconds=1))
    prepared = case.invoke(case.adapter("prepared"), request("status", repo_set.root, state, "closure"))
    records = repo_records(prepared.response)
    prepared_only = bool(records) and killed.returncode == -signal.SIGKILL and (prepared.response or {}).get("decision", {}).get("state") == "prepared" and all(ref_oid(repo, "refs/heads/main") == repo_set.bases[identifier] and not object_visible(repo, records[identifier].get("candidate_commit")) and object_visible(repo, records[identifier].get("candidate_commit"), state / "quarantine" / identifier / "objects") for identifier, repo in repo_set.repos.items())
    receipts = bool(records) and all(digest_ok(state, item.get("prepare_digest")) and digest_ok(state, item.get("quarantine_digest")) for item in records.values())
    prepared_candidates = {identifier: candidate(prepared.response, identifier) for identifier in repo_set.repos}
    for item in (prepared.response or {}).get("subtasks", []):
        if item.get("worktree"):
            shutil.rmtree(item["worktree"], ignore_errors=True)
    for identifier, repo in repo_set.repos.items():
        git(repo, "worktree", "prune", "--expire", "now")
        git(repo, "reflog", "expire", "--expire=now", "--all")
        git(repo, "prune", "--expire", "now")
        shutil.rmtree(state / "quarantine" / identifier, ignore_errors=True)
    time.sleep(1.25)
    recovered = case.invoke(case.adapter("recover"), request("recover", repo_set.root, state, "closure", owner=coordinator("owner-b", None, owner["fence"]), lease_seconds=1))
    rebuilt = all(object_visible(repo_set.repos[identifier], prepared_candidates[identifier]) for identifier in repo_set.repos) and all(candidate(recovered.response, identifier) == prepared_candidates[identifier] for identifier in repo_set.repos)
    published = bool(recovered.response and recovered.response.get("state") == "committed" and all_refs_at_candidates(repo_set, recovered.response) and links_match(repo_set, recovered.response))
    tree = tracked_tree(repo_set.repos["component"])
    binary_digest = hashlib.sha256(base64.b64decode("AP8QAGZpeHR1cmUtcHJpdmF0ZQ==")).hexdigest()
    component = repo_set.repos["component"]
    entry_ok = (component / "left.txt").read_text() == "RIGHT\n" and (component / "right.txt").read_text() == "LEFT\n" and "delete.txt" not in tree and tree.get("script.sh", {}).get("mode") == "100755" and tree.get("link", {}).get("mode") == "120000" and os.readlink(component / "link") == "right.txt" and hashlib.sha256((component / "binary.bin").read_bytes()).hexdigest() == binary_digest
    filtered = git(component, "show", "HEAD:filtered.txt") == "GITBLOB changed\n" and (component / "filtered.txt").read_text() == "WORKTREE changed\n"
    workers, tests, _events = event_counts(case, "closure", ("shape", "root-edit"))

    corrupt_set, corrupt_value = setup_shape_set(case, case.root / "corrupt", "corrupt")
    corrupt_state, corrupt_adapter = case.root / "corrupt-state", case.adapter("corrupt")
    corrupt_created = case.invoke(corrupt_adapter, request("create", corrupt_set.root, corrupt_state, "corrupt-closure", plan=corrupt_value, owner=coordinator("owner-c"), lease_seconds=5))
    corrupt_owner = owner_from(corrupt_created, "owner-c")
    corruption_closed = False
    if corrupt_owner:
        case.invoke(corrupt_adapter, request("run", corrupt_set.root, corrupt_state, "corrupt-closure", max_workers=2, crash={"after": "federation_prepared", "occurrence": 1, "mode": "exit75"}, owner=corrupt_owner, lease_seconds=5))
        corrupt_status = case.invoke(case.adapter("corrupt-status"), request("status", corrupt_set.root, corrupt_state, "corrupt-closure"))
        digest = repo_records(corrupt_status.response).get("component", {}).get("prepare_digest")
        path = object_path(corrupt_state, digest)
        before_refs = {identifier: refs(repo) for identifier, repo in corrupt_set.repos.items()}
        before_events = len(log_events(case.command_log))
        if path and path.is_file():
            path.write_bytes(b"corrupt repository prepare\n")
            result = case.invoke(case.adapter("corrupt-recover"), request("recover", corrupt_set.root, corrupt_state, "corrupt-closure", owner=corrupt_owner, lease_seconds=5))
            corruption_closed = bool(result.response and result.response.get("state") in {"blocked", "failed"} and before_refs == {identifier: refs(repo) for identifier, repo in corrupt_set.repos.items()} and len(log_events(case.command_log)) == before_events and not any(item.get("objects_promoted") for item in repo_records(result.response).values()))
    assertions = [
        assertion("CLOSURE-PREPARED-ONLY", 15, prepared_only, f"prepared_only={prepared_only}"),
        assertion("CLOSURE-PUBLIC-RECEIPTS", 15, receipts, f"receipts={receipts}"),
        assertion("CLOSURE-REBUILD-QUARANTINES", 15, rebuilt, f"rebuilt={rebuilt}"),
        assertion("CLOSURE-PUBLISH-GITLINK", 15, published, f"published={published}"),
        assertion("CLOSURE-ENTRY-FIDELITY", 15, entry_ok, f"entry_ok={entry_ok}"),
        assertion("CLOSURE-FILTER-FIDELITY", 10, filtered, f"filtered={filtered}"),
        assertion("CLOSURE-EFFECTS-ONCE", 5, workers == {"shape": 1, "root-edit": 1} and tests == 4, f"workers={workers} tests={tests}"),
        assertion("CLOSURE-CORRUPTION-CLOSED", 10, corruption_closed, f"closed={corruption_closed}"),
    ]
    lost = not entry_ok or any(count > 1 for count in workers.values()) or tests > 4
    return scored(case.case_id, assertions, cap=10 if lost else None, cap_reason="repository-set entry loss or duplicate replay" if lost else None, commands=case.invocations, duration=time.monotonic() - started)


def case_003(case: Case) -> dict[str, Any]:
    started = time.monotonic()
    repo_set = make_repo_set(
        case.root / "set", "root", {"root.txt": "root\n"},
        {
            "shape": ("modules/shape", {"one.txt": "ONE\n", "two.txt": "TWO\n", "script.sh": "#!/bin/sh\n", "filtered.txt": "WORKTREE\n", ".gitattributes": "filtered.txt filter=bench\n"}),
            "nested": ("modules/nested", {"nested-root.txt": "nested-root\n"}),
        },
        nested_component="nested",
    )
    filter_log = case.root / "filter.log"
    install_filter(repo_set.repos["shape"], filter_log)
    hook_logs = {identifier: case.root / f"{identifier}-commit-hooks.log" for identifier in repo_set.repos}
    ref_logs = {identifier: case.root / f"{identifier}-ref-hooks.log" for identifier in repo_set.repos}
    for identifier, repo in repo_set.repos.items():
        install_hooks(repo, hook_logs[identifier], ref_logs[identifier])
    shape_worker = case.spec("prefix-shape", {"actions": [{"op": "rename", "path": "one.txt", "to": "cycle.tmp"}, {"op": "rename", "path": "two.txt", "to": "one.txt"}, {"op": "rename", "path": "cycle.tmp", "to": "two.txt"}, {"op": "chmod", "path": "script.sh", "executable": True}, {"op": "symlink", "path": "current", "target": "two.txt"}, {"op": "write", "path": "filtered.txt", "content": "WORKTREE prefix\n"}]})
    shape_test = case.spec("prefix-shape-test", {"assertions": [{"op": "executable", "path": "script.sh"}, {"op": "symlink", "path": "current", "target": "two.txt"}]})
    nested_worker = case.spec("prefix-nested", {"actions": [{"op": "git", "cwd": "nested/lib", "argv": ["checkout", "-q", repo_set.nested_second]}]})
    nested_test = case.spec("prefix-nested-test", {"assertions": [{"op": "git_head", "path": "nested/lib", "oid": repo_set.nested_second}]})
    root_worker, root_test = simple_specs(case, "prefix-root", "root.txt", "root-prefix\n")
    tasks = [
        subtask("shape-task", "shape", [], ["one.txt", "two.txt", "cycle.tmp", "script.sh", "current", "filtered.txt"], case.command("worker", shape_worker), case.command("test", shape_test)),
        subtask("nested-task", "nested", [], ["nested/lib"], case.command("worker", nested_worker), case.command("test", nested_test)),
        subtask("root-task", "root", ["shape-task", "nested-task"], ["root.txt"], case.command("worker", root_worker), case.command("test", root_test)),
    ]
    value = plan(repository_descriptors(repo_set), tasks, integration_tests(case, {"shape": shape_test, "nested": nested_test, "root": root_test}), targets=all_targets(repo_set, absent_root_audit=True), participant_order=["shape", "nested", "root"], links=repo_set.links)
    state, adapter = case.root / "state", case.adapter("owner")
    created = case.invoke(adapter, request("create", repo_set.root, state, "prefix", plan=value, owner=coordinator("owner"), lease_seconds=5))
    owner = owner_from(created, "owner")
    if owner is None:
        return failed(case, "postdecision repository-set protocol unavailable")
    killed = case.invoke(adapter, request("run", repo_set.root, state, "prefix", max_workers=3, crash={"after": "participant_refs_committed", "occurrence": 1, "mode": "sigkill"}, owner=owner, lease_seconds=5))
    status_before_digest = state_digest(state)
    ref_lock = common_dir(repo_set.repos["nested"]) / "refs" / "heads" / "main.lock"
    ref_lock.parent.mkdir(parents=True, exist_ok=True)
    ref_lock.write_text("held by evaluator\n")
    status = case.invoke(case.adapter("status"), request("status", repo_set.root, state, "prefix"))
    ref_lock.unlink(missing_ok=True)
    status_pure = status.duration_seconds < 1 and state_digest(state) == status_before_digest
    completed = (status.response or {}).get("decision", {}).get("completed_participants", [])
    prefix_ok = killed.returncode == -signal.SIGKILL and (status.response or {}).get("decision", {}).get("state") == "commit" and completed == ["shape"] and ref_oid(repo_set.repos["shape"], "refs/heads/main") == candidate(status.response, "shape") and ref_oid(repo_set.repos["nested"], "refs/heads/main") == repo_set.bases["nested"] and ref_oid(repo_set.root, "refs/heads/main") == repo_set.bases["root"]
    first_candidate = candidate(status.response, "shape")
    removed = remove_loose_object(repo_set.repos["shape"], first_candidate)
    missing = removed and not object_visible(repo_set.repos["shape"], first_candidate)
    first_ref_hooks = ref_logs["shape"].read_text().splitlines() if ref_logs["shape"].exists() else []
    first_commit_hooks = hook_logs["shape"].read_text().splitlines() if hook_logs["shape"].exists() else []
    filter_before = filter_log.read_text().splitlines() if filter_log.exists() else []
    recovered = case.invoke(case.adapter("recover"), request("recover", repo_set.root, state, "prefix", owner=owner, lease_seconds=5))
    rehydrated = missing and object_visible(repo_set.repos["shape"], first_candidate) and candidate(recovered.response, "shape") == first_candidate
    suffix = bool(recovered.response and recovered.response.get("state") == "committed" and all_refs_at_candidates(repo_set, recovered.response, audit=True) and (recovered.response.get("decision", {}).get("completed_participants") == ["shape", "nested", "root"]))
    tree_shape = tracked_tree(repo_set.repos["shape"])
    tree_nested = tracked_tree(repo_set.repos["nested"])
    modes_links = tree_shape.get("script.sh", {}).get("mode") == "100755" and tree_shape.get("current", {}).get("mode") == "120000" and tree_nested.get("nested/lib", {}).get("mode") == "160000" and tree_nested.get("nested/lib", {}).get("oid") == repo_set.nested_second and links_match(repo_set, recovered.response)
    filtered = git(repo_set.repos["shape"], "show", "HEAD:filtered.txt") == "GITBLOB prefix\n" and (repo_set.repos["shape"] / "filtered.txt").read_text() == "WORKTREE prefix\n"
    first_hooks_stable = first_ref_hooks == (ref_logs["shape"].read_text().splitlines() if ref_logs["shape"].exists() else []) and first_commit_hooks == (hook_logs["shape"].read_text().splitlines() if hook_logs["shape"].exists() else [])
    workers, tests, _events = event_counts(case, "prefix", ("shape-task", "nested-task", "root-task"))
    filter_after = filter_log.read_text().splitlines() if filter_log.exists() else []
    one = case.invoke(case.adapter("one"), request("status", repo_set.root, state, "prefix"))
    two = case.invoke(case.adapter("two"), request("status", repo_set.root, state, "prefix"))
    status_stable = bool(one.response and two.response and one.response.get("ledger", {}).get("digest") == two.response.get("ledger", {}).get("digest") and filter_log.read_text().splitlines() == filter_after)
    clean = all(len(worktrees(repo)) == 1 and git(repo, "status", "--porcelain") == "" for repo in repo_set.repos.values())
    assertions = [
        assertion("PREFIX-DURABLE-DECISION", 15, prefix_ok, f"prefix={completed} exit={killed.returncode}"),
        assertion("PREFIX-STATUS-PURE", 10, status_pure, f"duration={status.duration_seconds:.3f} pure={status_pure}"),
        assertion("PREFIX-REHYDRATE-COMMITTED", 15, rehydrated, f"missing={missing} rehydrated={rehydrated}"),
        assertion("PREFIX-ROLL-FORWARD-SUFFIX", 20, suffix, f"suffix={suffix}"),
        assertion("PREFIX-MODES-AND-GITLINKS", 15, modes_links, f"modes_links={modes_links}"),
        assertion("PREFIX-HOOKS-FILTERS-ONCE", 15, first_hooks_stable and filtered and filter_before and all(count == 1 for count in workers.values()) and tests == 6, f"first_hooks={first_hooks_stable} filtered={filtered} workers={workers} tests={tests}"),
        assertion("PREFIX-CLEAN-STABLE", 10, clean and status_stable, f"clean={clean} stable={status_stable}"),
    ]
    lost = not modes_links or any(count > 1 for count in workers.values()) or tests > 6
    return scored(case.case_id, assertions, cap=10 if lost else None, cap_reason="gitlink/entry loss or duplicate postdecision effect" if lost else None, commands=case.invocations, duration=time.monotonic() - started)


def case_004(case: Case) -> dict[str, Any]:
    started = time.monotonic()
    repo_set = make_repo_set(case.root / "set", "root", {"root.txt": "root\n"}, {"component": ("vendor/component", {"pkg/code.py": "base\n"})})
    component = repo_set.repos["component"]
    foreign_path = case.root / "foreign-component-worktree"
    git(component, "branch", "foreign-component", repo_set.bases["component"])
    git(component, "worktree", "add", "-q", str(foreign_path), "foreign-component")
    git(component, "worktree", "lock", "--reason", "FOREIGN_LEDGER_SECRET component lock", str(foreign_path))
    hook_log = case.root / "conflict-hooks.log"
    install_hooks(component, hook_log)
    before = {identifier: exact_repo_state(repo) for identifier, repo in repo_set.repos.items()}
    rename = case.spec("conflict-rename", {"actions": [{"op": "rename", "path": "pkg/code.py", "to": "pkg/renamed.py"}]})
    delete = case.spec("conflict-delete", {"actions": [{"op": "delete", "path": "pkg/code.py"}]})
    edit = case.spec("conflict-edit", {"actions": [{"op": "write", "path": "pkg/code.py", "content": "edited\n"}]})
    local_test = case.spec("conflict-test", {"assertions": [{"op": "exists", "path": "pkg"}]})
    root_worker, root_test = simple_specs(case, "conflict-root", "root.txt", "root-worker\n")
    tasks = [
        subtask("rename", "component", [], ["pkg/code.py", "pkg/renamed.py"], case.command("worker", rename), case.command("test", local_test)),
        subtask("delete", "component", [], ["pkg/code.py"], case.command("worker", delete), case.command("test", local_test)),
        subtask("edit", "component", [], ["pkg/code.py"], case.command("worker", edit), case.command("test", local_test)),
        subtask("root-work", "root", [], ["root.txt"], case.command("worker", root_worker), case.command("test", root_test)),
    ]
    value = plan(repository_descriptors(repo_set), tasks, integration_tests(case, {"component": local_test, "root": root_test}), integration_order=["rename", "delete", "edit", "root-work"], targets=all_targets(repo_set), participant_order=["component", "root"], links=repo_set.links)
    state, adapter = case.root / "state", case.adapter("conflict")
    created = case.invoke(adapter, request("create", repo_set.root, state, "set-conflict", plan=value, owner=coordinator("owner"), lease_seconds=5))
    owner = owner_from(created, "owner")
    if owner is None:
        return failed(case, "repository-set conflict protocol unavailable")

    _origin, disjoint_repo, disjoint_base = make_repo(case.root / "disjoint", {"isolated.txt": "isolated\n"})
    dw, dt = simple_specs(case, "conflict-disjoint", "isolated.txt", "isolated-committed\n")
    disjoint_plan = plan(
        [{"id": "isolated", "path": str(disjoint_repo), "base_revision": disjoint_base, "base_state_policy": "require_clean", "role": "root"}],
        [subtask("isolated-task", "isolated", [], ["isolated.txt"], case.command("worker", dw), case.command("test", dt))],
        [{"repository_id": "isolated", "command": case.command("test", dt)}], targets=[target("isolated", "refs/heads/main", disjoint_base)], participant_order=["isolated"],
    )
    disjoint_state, disjoint_adapter = case.root / "disjoint-state", case.adapter("disjoint")
    disjoint_created = case.invoke(disjoint_adapter, request("create", disjoint_repo, disjoint_state, "isolated-plan", plan=disjoint_plan, owner=coordinator("isolated-owner"), lease_seconds=5))
    disjoint_owner = owner_from(disjoint_created, "isolated-owner")
    disjoint_run = case.invoke(disjoint_adapter, request("run", disjoint_repo, disjoint_state, "isolated-plan", max_workers=1, owner=disjoint_owner, lease_seconds=5)) if disjoint_owner else None
    disjoint_candidate = candidate(disjoint_run.response if disjoint_run else None, "isolated")
    disjoint_ok = bool(disjoint_run and disjoint_run.response and disjoint_run.response.get("state") == "committed" and ref_oid(disjoint_repo, "refs/heads/main") == disjoint_candidate)

    ran = case.invoke(adapter, request("run", repo_set.root, state, "set-conflict", max_workers=4, owner=owner, lease_seconds=5))
    after_run = {identifier: exact_repo_state(repo) for identifier, repo in repo_set.repos.items()}
    hooks_first = hook_log.read_text().splitlines() if hook_log.exists() else []
    replay = case.invoke(case.adapter("replay"), request("recover", repo_set.root, state, "set-conflict", owner=owner, lease_seconds=5))
    hooks_second = hook_log.read_text().splitlines() if hook_log.exists() else []
    decision = (ran.response or {}).get("decision", {})
    no_decision = ran.response is not None and ran.response.get("state") in {"blocked", "failed"} and decision.get("state") in {"none", "aborted"} and not decision.get("digest")
    unchanged = all(after_run[identifier] == before[identifier] for identifier in repo_set.repos)
    variants = [item for item in (ran.response or {}).get("subtasks", []) if item.get("repository_id") == "component" and item.get("commit") and isinstance(item.get("snapshot"), dict)]
    records = repo_records(ran.response)
    quarantined = not any(item.get("objects_promoted") for item in records.values())
    stable = bool(replay.response and replay.response.get("reason") == ran.response.get("reason") and replay.response.get("decision") == decision and repo_records(replay.response) == records)
    foreign_before = [item for item in before["component"]["worktrees"] if item.get("worktree") == str(foreign_path)]
    external = advance_ref(repo_set.root, "refs/heads/release", repo_set.bases["root"], "external after set conflict")
    rolled = case.invoke(case.adapter("rollback"), request("rollback", repo_set.root, state, "set-conflict", owner=owner_from(replay, "owner") or owner, lease_seconds=5))
    foreign_after = [item for item in worktrees(component) if item.get("worktree") == str(foreign_path)]
    ownership = ref_oid(repo_set.root, "refs/heads/release") == external and ref_oid(disjoint_repo, "refs/heads/main") == disjoint_candidate and foreign_before == foreign_after and foreign_path.exists()
    assertions = [
        assertion("CONFLICT-SET-NO-DECISION", 20, no_decision, f"state={ran.response and ran.response.get('state')} decision={decision.get('state')}"),
        assertion("CONFLICT-ALL-REFS-UNCHANGED", 15, unchanged, f"unchanged={unchanged}"),
        assertion("CONFLICT-VARIANTS-QUARANTINED", 15, len(variants) == 3 and quarantined, f"variants={len(variants)} quarantined={quarantined}"),
        assertion("CONFLICT-STABLE-EFFECTS", 10, stable and hooks_second == hooks_first and len(hooks_first) == 3, f"stable={stable} hooks={len(hooks_second)}"),
        assertion("CONFLICT-FOREIGN-LOCK", 10, foreign_before == foreign_after and foreign_path.exists(), f"preserved={foreign_before == foreign_after}"),
        assertion("CONFLICT-DISJOINT-COMMITS", 15, disjoint_ok, f"disjoint={disjoint_ok}"),
        assertion("CONFLICT-ROLLBACK-OWNERSHIP", 15, rolled.response is not None and rolled.response.get("state") == "rolled_back" and ownership, f"state={rolled.response and rolled.response.get('state')} ownership={ownership}"),
    ]
    user_loss = not ownership or foreign_before != foreign_after
    return scored(case.case_id, assertions, cap=0 if user_loss else None, cap_reason="foreign/disjoint/external ownership changed" if user_loss else None, commands=case.invocations, duration=time.monotonic() - started)


def dirty_repository(repo: Path, prefix: str) -> None:
    tracked = repo / f"{prefix}-tracked.txt"
    tracked.write_text(f"{prefix}-staged\n")
    git(repo, "add", tracked.name)
    tracked.write_text(f"{prefix}-unstaged\n")
    intent = repo / f"{prefix}-intent.txt"
    intent.write_text(f"{prefix}-intent\n")
    git(repo, "add", "-N", intent.name)
    (repo / f"{prefix}-user.bin").write_bytes(b"\x00\xffUSER_SENTINEL_PRIVATE\x00" + prefix.encode())
    executable = repo / f"{prefix}-exec.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    (repo / f"{prefix}-link").symlink_to(tracked.name)
    base_blob = git(repo, "rev-parse", f"HEAD:{tracked.name}").strip()
    ours = git(repo, "hash-object", "-w", "--stdin", input_text=f"{prefix}-ours\n").strip()
    theirs = git(repo, "hash-object", "-w", "--stdin", input_text=f"{prefix}-theirs\n").strip()
    index_info = f"100644 {base_blob} 1\t{prefix}-stages.txt\n100644 {ours} 2\t{prefix}-stages.txt\n100644 {theirs} 3\t{prefix}-stages.txt\n"
    git(repo, "update-index", "--index-info", input_text=index_info)
    (repo / f"{prefix}-stages.txt").write_text(f"{prefix}-working\n")


def case_005(case: Case) -> dict[str, Any]:
    started = time.monotonic()
    repo_set = make_repo_set(
        case.root / "set", "root", {"root-work.txt": "root\n", "root-tracked.txt": "root-base\n"},
        {"component": ("vendor/component", {"component-work.txt": "component\n", "component-tracked.txt": "component-base\n"})},
    )
    git(repo_set.root, "update-ref", "refs/bench/root-user", repo_set.bases["root"])
    git(repo_set.repos["component"], "update-ref", "refs/bench/component-user", repo_set.bases["component"])
    git(repo_set.repos["component"], "checkout", "--detach", "-q", repo_set.bases["component"])
    dirty_repository(repo_set.root, "root")
    dirty_repository(repo_set.repos["component"], "component")
    before = {identifier: exact_repo_state(repo) for identifier, repo in repo_set.repos.items()}
    rw, rt = simple_specs(case, "reverse-root", "root-work.txt", "root-candidate\n")
    cw, ct = simple_specs(case, "reverse-component", "component-work.txt", "component-candidate\n")
    tasks = [
        subtask("root-task", "root", [], ["root-work.txt"], case.command("worker", rw), case.command("test", rt)),
        subtask("component-task", "component", [], ["component-work.txt"], case.command("worker", cw), case.command("test", ct)),
    ]
    value = plan(repository_descriptors(repo_set, {"root", "component"}), tasks, integration_tests(case, {"root": rt, "component": ct}), targets=all_targets(repo_set), participant_order=["component", "root"], links=repo_set.links)
    state, adapter = case.root / "state", case.adapter("owner")
    created = case.invoke(adapter, request("create", repo_set.root, state, "reverse", plan=value, owner=coordinator("owner-a"), lease_seconds=1))
    owner = owner_from(created, "owner-a")
    if owner is None:
        return failed(case, "dirty repository-set create unavailable")
    ran = case.invoke(adapter, request("run", repo_set.root, state, "reverse", max_workers=2, owner=owner, lease_seconds=1))
    current = owner_from(ran, "owner-a") or owner
    set_committed = bool(ran.response and ran.response.get("state") == "committed" and all_refs_at_candidates(repo_set, ran.response) and links_match(repo_set, ran.response) and all(isinstance(item.get("base_snapshot"), dict) for item in repo_records(ran.response).values()))
    component_candidate = candidate(ran.response, "component")
    external = advance_ref(repo_set.repos["component"], "refs/heads/release", component_candidate or repo_set.bases["component"], "external after set commit") if component_candidate else None
    refs_before_blocked = {identifier: refs(repo) for identifier, repo in repo_set.repos.items()}
    blocked = case.invoke(case.adapter("blocked"), request("rollback", repo_set.root, state, "reverse", owner=current, lease_seconds=1))
    no_partial = refs_before_blocked == {identifier: refs(repo) for identifier, repo in repo_set.repos.items()}
    drift_abort = bool(blocked.response and blocked.response.get("state") in {"blocked", "failed"} and "ref_drift" in str(blocked.response.get("reason")) and no_partial)
    retry_owner = owner_from(blocked, "owner-a") or current
    if external and component_candidate:
        git(repo_set.repos["component"], "update-ref", "refs/heads/release", component_candidate, external)
    killed = case.invoke(case.adapter("hard-reverse"), request("rollback", repo_set.root, state, "reverse", crash={"after": "participant_rollback_committed", "occurrence": 1, "mode": "sigkill"}, owner=retry_owner, lease_seconds=1))
    partial_status = case.invoke(case.adapter("partial-status"), request("status", repo_set.root, state, "reverse"))
    completed = (partial_status.response or {}).get("decision", {}).get("completed_participants", [])
    reverse_prefix = killed.returncode == -signal.SIGKILL and (partial_status.response or {}).get("decision", {}).get("state") == "rollback" and completed == ["root"]
    time.sleep(1.25)
    recovered = case.invoke(case.adapter("takeover"), request("recover", repo_set.root, state, "reverse", owner=coordinator("owner-b", None, retry_owner["fence"]), lease_seconds=1))
    exact = {identifier: exact_repo_state(repo) for identifier, repo in repo_set.repos.items()} == before
    suffix = bool(recovered.response and recovered.response.get("state") == "rolled_back" and recovered.response.get("coordination", {}).get("fence") == retry_owner["fence"] + 1)
    workers, tests, _events = event_counts(case, "reverse", ("root-task", "component-task"))
    one = case.invoke(case.adapter("one"), request("status", repo_set.root, state, "reverse"))
    two = case.invoke(case.adapter("two"), request("status", repo_set.root, state, "reverse"))
    stable = bool(one.response and two.response and one.response.get("ledger", {}).get("digest") == two.response.get("ledger", {}).get("digest"))
    assertions = [
        assertion("REVERSE-SET-COMMIT", 15, set_committed, f"committed={set_committed}"),
        assertion("REVERSE-DRIFT-ABORT-ALL", 20, drift_abort, f"drift_abort={drift_abort}"),
        assertion("REVERSE-DECISION-PREFIX", 15, reverse_prefix, f"exit={killed.returncode} prefix={completed}"),
        assertion("REVERSE-TAKEOVER-SUFFIX", 15, suffix, f"suffix={suffix}"),
        assertion("REVERSE-EXACT-TWO-REPOSITORIES", 25, exact, f"exact={exact}"),
        assertion("REVERSE-ONCE-STABLE", 10, workers == {"root-task": 1, "component-task": 1} and tests == 4 and stable, f"workers={workers} tests={tests} stable={stable}"),
    ]
    return scored(case.case_id, assertions, cap=0 if not exact else None, cap_reason="two-repository dirty/index/gitlink state not restored" if not exact else None, commands=case.invocations, duration=time.monotonic() - started)


def case_006(case: Case) -> dict[str, Any]:
    started = time.monotonic()
    alias_set = make_repo_set(case.root / "alias", "root", {"root.txt": "root\n"}, {"component": ("vendor/component", {"component.txt": "component\n"})})
    alias_path = case.root / "component-alias"
    alias_path.symlink_to(alias_set.repos["component"], target_is_directory=True)
    aw, at = simple_specs(case, "alias", "root.txt", "changed\n")
    alias_repositories = repository_descriptors(alias_set) + [{"id": "component-alias", "path": str(alias_path), "base_revision": alias_set.bases["component"], "base_state_policy": "require_clean", "role": "component"}]
    alias_plan = plan(alias_repositories, [subtask("alias-task", "root", [], ["root.txt"], case.command("worker", aw), case.command("test", at))], [{"repository_id": "root", "command": case.command("test", at)}], targets=all_targets(alias_set), participant_order=["component", "component-alias", "root"], links=alias_set.links)
    alias_state = case.root / "alias-state"
    alias_result = case.invoke(case.adapter("alias"), request("create", alias_set.root, alias_state, "alias-plan", plan=alias_plan, owner=coordinator("alias-owner"), lease_seconds=5))
    alias_ok = bool(alias_result.response and alias_result.response.get("state") in {"blocked", "failed"} and not alias_state.exists())

    foreign_set = make_repo_set(case.root / "foreign", "root", {"root.txt": "root\n"}, {"component": ("vendor/component", {"component.txt": "component\n"})})
    foreign_path = common_dir(foreign_set.repos["component"]) / "aider" / "transactions" / "admission.json"
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_path.write_bytes(b'{"schema_version":3,"transaction_id":"FOREIGN_LEDGER_SECRET","expires":9999999999999,"fence":7}\n')
    foreign_before = foreign_path.read_bytes()
    fw, ft = simple_specs(case, "foreign", "root.txt", "changed\n")
    foreign_plan = plan(repository_descriptors(foreign_set), [subtask("foreign-task", "root", [], ["root.txt"], case.command("worker", fw), case.command("test", ft))], integration_tests(case, {"root": ft, "component": ft}), targets=all_targets(foreign_set), participant_order=["component", "root"], links=foreign_set.links)
    foreign_state = case.root / "foreign-state"
    foreign_result = case.invoke(case.adapter("foreign"), request("create", foreign_set.root, foreign_state, "foreign-plan", plan=foreign_plan, owner=coordinator("foreign-owner"), lease_seconds=5))
    foreign_ok = bool(foreign_result.response and foreign_result.response.get("state") in {"blocked", "failed"} and foreign_path.read_bytes() == foreign_before and not foreign_state.exists())

    corrupt_set, corrupt_value = setup_shape_set(case, case.root / "corrupt", "guard-corrupt")
    corrupt_state, corrupt_adapter = case.root / "corrupt-state", case.adapter("corrupt")
    corrupt_created = case.invoke(corrupt_adapter, request("create", corrupt_set.root, corrupt_state, "guard-corrupt", plan=corrupt_value, owner=coordinator("owner-c"), lease_seconds=5))
    corrupt_owner = owner_from(corrupt_created, "owner-c")
    corruption_ok = False
    if corrupt_owner:
        case.invoke(corrupt_adapter, request("run", corrupt_set.root, corrupt_state, "guard-corrupt", max_workers=2, crash={"after": "federation_prepared", "occurrence": 1, "mode": "exit75"}, owner=corrupt_owner, lease_seconds=5))
        prepared = case.invoke(case.adapter("corrupt-status"), request("status", corrupt_set.root, corrupt_state, "guard-corrupt"))
        path = object_path(corrupt_state, (prepared.response or {}).get("decision", {}).get("digest"))
        before_refs = {identifier: refs(repo) for identifier, repo in corrupt_set.repos.items()}
        before_events = len(log_events(case.command_log))
        if path and path.is_file():
            path.write_bytes(b"corrupt global decision\n")
            result = case.invoke(case.adapter("corrupt-recover"), request("recover", corrupt_set.root, corrupt_state, "guard-corrupt", owner=corrupt_owner, lease_seconds=5))
            corruption_ok = bool(result.response and result.response.get("state") in {"blocked", "failed"} and before_refs == {identifier: refs(repo) for identifier, repo in corrupt_set.repos.items()} and len(log_events(case.command_log)) == before_events and not any(item.get("objects_promoted") for item in repo_records(result.response).values()))

    obstruct_set, obstruct_value = setup_shape_set(case, case.root / "obstruct", "guard-obstruct")
    obstruct_state, obstruct_adapter = case.root / "obstruct-state", case.adapter("obstruct")
    obstruct_created = case.invoke(obstruct_adapter, request("create", obstruct_set.root, obstruct_state, "guard-obstruct", plan=obstruct_value, owner=coordinator("owner-o"), lease_seconds=5))
    obstruct_owner = owner_from(obstruct_created, "owner-o")
    obstruction_ok = mixture_ok = False
    if obstruct_owner:
        case.invoke(obstruct_adapter, request("run", obstruct_set.root, obstruct_state, "guard-obstruct", max_workers=2, crash={"after": "participant_refs_committed", "occurrence": 1, "mode": "sigkill"}, owner=obstruct_owner, lease_seconds=5))
        partial = case.invoke(case.adapter("obstruct-status"), request("status", obstruct_set.root, obstruct_state, "guard-obstruct"))
        component_candidate = candidate(partial.response, "component")
        root_candidate = candidate(partial.response, "root")
        if component_candidate and root_candidate:
            git(obstruct_set.repos["component"], "update-ref", "refs/heads/release", obstruct_set.bases["component"], component_candidate)
            external = advance_ref(obstruct_set.root, "refs/heads/main", obstruct_set.bases["root"], "external obstruction")
            before = {identifier: refs(repo) for identifier, repo in obstruct_set.repos.items()}
            result = case.invoke(case.adapter("obstruct-recover"), request("recover", obstruct_set.root, obstruct_state, "guard-obstruct", owner=obstruct_owner, lease_seconds=5))
            after = {identifier: refs(repo) for identifier, repo in obstruct_set.repos.items()}
            obstruction_ok = bool(result.response and result.response.get("state") in {"blocked", "failed"} and "decision_obstructed" in str(result.response.get("reason")) and before == after and ref_oid(obstruct_set.root, "refs/heads/main") == external and ref_oid(obstruct_set.repos["component"], "refs/heads/main") == component_candidate)
            mixture_ok = obstruction_ok and ref_oid(obstruct_set.repos["component"], "refs/heads/release") == obstruct_set.bases["component"] and not object_visible(obstruct_set.root, root_candidate)

    status_set, status_value = setup_shape_set(case, case.root / "status", "guard-status")
    status_state, status_adapter = case.root / "status-state", case.adapter("status-owner")
    status_created = case.invoke(status_adapter, request("create", status_set.root, status_state, "guard-status", plan=status_value, owner=coordinator("owner-s"), lease_seconds=5))
    status_owner = owner_from(status_created, "owner-s")
    status_pure = False
    if status_owner:
        case.invoke(status_adapter, request("run", status_set.root, status_state, "guard-status", max_workers=2, crash={"after": "federation_prepared", "occurrence": 1, "mode": "exit75"}, owner=status_owner, lease_seconds=5))
        lock = common_dir(status_set.root) / "refs" / "heads" / "main.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("held\n")
        before_state = state_digest(status_state)
        admissions = {identifier: (common_dir(repo) / "aider" / "transactions" / "admission.json").read_bytes() for identifier, repo in status_set.repos.items()}
        one = case.invoke(case.adapter("status-one"), request("status", status_set.root, status_state, "guard-status"))
        two = case.invoke(case.adapter("status-two"), request("status", status_set.root, status_state, "guard-status"))
        status_pure = bool(one.response and two.response and one.duration_seconds < 1 and two.duration_seconds < 1 and before_state == state_digest(status_state) and admissions == {identifier: (common_dir(repo) / "aider" / "transactions" / "admission.json").read_bytes() for identifier, repo in status_set.repos.items()})
        lock.unlink(missing_ok=True)

    compat_set = make_repo_set(case.root / "compat", "root", {"normal.txt": "WORKTREE\n", ".gitattributes": "normal.txt filter=bench\n"}, {"component": ("vendor/component", {"component.txt": "component\n"})})
    install_filter(compat_set.root)
    hook_log = case.root / "compat-hooks.log"
    install_hooks(compat_set.root, hook_log)
    compat_adapter = case.adapter("compat-env")
    version = run([str(case.python), "-m", "aider", "--version"], cwd=compat_set.root, env=compat_adapter.env, timeout=60)
    (compat_set.root / "normal.txt").write_text("WORKTREE ordinary\n")
    git(compat_set.root, "add", "normal.txt")
    git(compat_set.root, "commit", "-q", "-m", "ordinary")
    filtered = git(compat_set.root, "show", "HEAD:normal.txt") == "GITBLOB ordinary\n" and hook_log.read_text().splitlines() == ["ordinary"]
    submodule_status = run(["git", "-c", "protocol.file.allow=always", "submodule", "status", "--recursive"], cwd=compat_set.root)
    auxiliary = case.root / "ordinary-worktree"
    git(compat_set.root, "branch", "ordinary-worktree", "HEAD")
    lifecycle = [
        run(["git", "worktree", "add", "-q", str(auxiliary), "ordinary-worktree"], cwd=compat_set.root),
        run(["git", "worktree", "lock", "--reason", "ordinary", str(auxiliary)], cwd=compat_set.root),
        run(["git", "worktree", "unlock", str(auxiliary)], cwd=compat_set.root),
        run(["git", "worktree", "remove", str(auxiliary)], cwd=compat_set.root),
    ]
    git_compat = filtered and submodule_status.returncode == 0 and all(item.returncode == 0 for item in lifecycle) and len(worktrees(compat_set.root)) == 1
    overwrite = not foreign_ok or (obstruction_ok and not mixture_ok)
    assertions = [
        assertion("GUARD-ALIAS-FOREIGN-ADMISSION", 15, alias_ok and foreign_ok, f"alias={alias_ok} foreign={foreign_ok}"),
        assertion("GUARD-DECISION-CORRUPTION", 15, corruption_ok, f"closed={corruption_ok}"),
        assertion("GUARD-POSTDECISION-OBSTRUCTION", 20, obstruction_ok, f"obstruction={obstruction_ok}"),
        assertion("GUARD-WITHIN-PARTICIPANT-MIXTURE", 10, mixture_ok, f"mixture={mixture_ok}"),
        assertion("GUARD-STATUS-PURITY", 15, status_pure, f"status_pure={status_pure}"),
        assertion("COMPAT-AIDER-VERSION", 10, version.returncode == 0 and bool(version.stdout.strip()), f"exit={version.returncode}"),
        assertion("COMPAT-GIT-SUBMODULE-WORKTREE", 15, git_compat, f"git_compat={git_compat}"),
    ]
    return scored(case.case_id, assertions, cap=0 if overwrite else None, cap_reason="foreign admission or postdecision writer overwritten/compensated" if overwrite else None, commands=case.invocations, duration=time.monotonic() - started)


SCENARIOS: dict[str, Callable[[Case], dict[str, Any]]] = {
    "test_001": case_001,
    "test_002": case_002,
    "test_003": case_003,
    "test_004": case_004,
    "test_005": case_005,
    "test_006": case_006,
}


def validate_result_manifest(value: dict[str, Any]) -> list[str]:
    case_id = value.get("case_id")
    expected = ASSERTION_MANIFEST.get(case_id)
    if expected is None:
        return [f"unknown case manifest {case_id!r}"]
    actual = tuple((item.get("id"), item.get("points")) for item in value.get("assertions", []))
    errors = []
    if actual != expected:
        errors.append(f"assertion manifest mismatch for {case_id}")
    if value.get("maximum") != 100 or not isinstance(value.get("score"), int) or not 0 <= value["score"] <= 100:
        errors.append(f"invalid score envelope for {case_id}")
    raw = 0
    for item in value.get("assertions", []):
        if item.get("earned") not in {0, item.get("points")} or bool(item.get("passed")) != (item.get("earned") == item.get("points")):
            errors.append(f"assertion accounting mismatch: {item.get('id')}")
        raw += int(item.get("earned", 0))
    if value.get("raw_score") != raw:
        errors.append(f"raw score mismatch for {case_id}")
    cap = value.get("local_integrity_cap")
    expected_score = min(raw, cap) if isinstance(cap, int) else raw
    if value.get("score") != expected_score:
        errors.append(f"cap accounting mismatch for {case_id}")
    return errors


def evaluate(case_id: str, python: Path, source: Path, output: Path) -> dict[str, Any]:
    if case_id not in SCENARIOS:
        raise HarnessError(f"unknown case: {case_id}")
    if output.exists() and any(output.iterdir()):
        raise HarnessError(f"refusing nonempty case output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    case = Case(case_id, python, source, output)
    try:
        value = SCENARIOS[case_id](case)
        errors = validate_result_manifest(value)
        if errors:
            raise HarnessError("; ".join(errors))
        return value
    except Exception as exc:
        value = failed(case, f"case-local exception: {type(exc).__name__}: {exc}")
        value["errors"] = [{"type": type(exc).__name__, "message": str(exc)[-2000:]}]
        return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASE_IDS, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    value = evaluate(args.case, args.python.resolve(), args.source.resolve(), args.output_dir.resolve())
    write_json(args.result.resolve(), value)
    print(json.dumps(value, indent=2))
    return 0 if value["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
