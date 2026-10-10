"""Shared deterministic runner and objective scoring for OpenWiki edit cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


TIMEOUT_SECONDS = 600
MEMORY_LIMIT_MB = 4096
# English release: the pinned input/repository is the en-upstream a0 (630eb9ec).
# Paper as-run (zh-asrun a0) value: 9f89b2c56a072f2e5c63d3b60bd6e7c7e9f56861ee1500e0925603ab938e1160
PINNED_SOURCE_TREE_HASH = "092844ed148d028bf7205ce5d98aff2a77ff4d80842326ab249e7bc5182bcc68"
REQUIRED_DELIVERY = {"solution.patch", "edit_report.json", "run_report.json"}
ALLOWED_PATCH_PREFIXES = ("src/", "test/", "openwiki/", "examples/", "skills/")
ALLOWED_PATCH_FILES = {
    "README.md",
    "CHANGELOG.md",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "tsconfig.json",
    "tsconfig.client.json",
    "tsconfig.eslint.json",
}
FORBIDDEN_PATCH_PREFIXES = (".git/", ".github/", ".claude/", "evals/")
EXAMPLE_RE = re.compile(
    r"<!--\s*openwiki:example\s+(\{.*?\})\s*-->\s*"
    r"```[^\n]*\n(.*?)\n```",
    re.DOTALL,
)
GENERATED_RE = re.compile(
    r"(<!--\s*openwiki:generated:start\s+id=\"([^\"]+)\"\s*-->).*?"
    r"(<!--\s*openwiki:generated:end\s+id=\"\2\"\s*-->)",
    re.DOTALL,
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    runtime_seconds: float
    peak_memory_mb: float
    timed_out: bool = False
    memory_exceeded: bool = False

    def evidence(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "runtime_seconds": round(self.runtime_seconds, 3),
            "peak_memory_mb": round(self.peak_memory_mb, 3),
            "timed_out": self.timed_out,
            "memory_exceeded": self.memory_exceeded,
        }


@dataclass
class Assertion:
    assertion_id: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.assertion_id,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class PreparedCandidate:
    source_dir: Path
    patch_paths: list[str]
    setup_results: list[CommandResult]
    delivery: dict[str, Any]


@dataclass
class CaseResult:
    case_id: str
    valid: bool
    score: float
    dimension_scores: dict[str, float]
    assertions: list[Assertion] = field(default_factory=list)
    invocation: CommandResult | None = None
    replay_invocation: CommandResult | None = None
    worktree: str | None = None
    safety_ceiling_applied: bool = False
    scenario_invocations: list[CommandResult] = field(default_factory=list)
    availability_ceiling_applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "valid": self.valid,
            "score": round(self.score, 2),
            "dimension_scores": {
                key: round(value, 2) for key, value in self.dimension_scores.items()
            },
            "assertions": [item.as_dict() for item in self.assertions],
            "invocation": self.invocation.evidence() if self.invocation else None,
            "replay_invocation": (
                self.replay_invocation.evidence() if self.replay_invocation else None
            ),
            "scenario_invocations": [item.evidence() for item in self.scenario_invocations],
            "worktree": self.worktree,
            "safety_ceiling_applied": self.safety_ceiling_applied,
            "availability_ceiling_applied": self.availability_ceiling_applied,
        }


class HarnessError(RuntimeError):
    """A candidate-level validity error."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read valid UTF-8 JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"expected JSON object in {path}")
    return value


def run_limited(
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    peak_kb = 0
    timed_out = False
    memory_exceeded = False
    while process.poll() is None:
        peak_kb = max(peak_kb, process_tree_rss_kb(process.pid))
        if time.monotonic() - started > timeout:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        if peak_kb > MEMORY_LIMIT_MB * 1024:
            memory_exceeded = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    peak_kb = max(peak_kb, process_tree_rss_kb(process.pid))
    return CommandResult(
        argv=argv,
        cwd=str(cwd),
        exit_code=124 if timed_out else (125 if memory_exceeded else int(process.returncode or 0)),
        stdout=stdout,
        stderr=stderr,
        runtime_seconds=time.monotonic() - started,
        peak_memory_mb=peak_kb / 1024.0,
        timed_out=timed_out,
        memory_exceeded=memory_exceeded,
    )


def process_tree_rss_kb(root_pid: int) -> int:
    processes: dict[int, tuple[int, int]] = {}
    proc_root = Path("/proc")
    if not proc_root.exists():
        return 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
            ppid_match = re.search(r"^PPid:\s+(\d+)", status, re.MULTILINE)
            rss_match = re.search(r"^VmRSS:\s+(\d+)\s+kB", status, re.MULTILINE)
            if ppid_match:
                processes[int(entry.name)] = (
                    int(ppid_match.group(1)),
                    int(rss_match.group(1)) if rss_match else 0,
                )
        except (OSError, ValueError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in processes.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return sum(processes.get(pid, (0, 0))[1] for pid in descendants)


def safe_relative_path(raw: str) -> str:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise HarnessError(f"unsafe patch path: {raw!r}")
    return path.as_posix()


def parse_patch_paths(patch_text: str) -> list[str]:
    paths: set[str] = set()
    for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", patch_text, re.MULTILINE):
        for raw in match.groups():
            paths.add(safe_relative_path(raw))
    if not paths:
        raise HarnessError("solution.patch has no diff --git file headers")
    for path in paths:
        if path.startswith(FORBIDDEN_PATCH_PREFIXES):
            raise HarnessError(f"forbidden patch path: {path}")
        if path not in ALLOWED_PATCH_FILES and not path.startswith(ALLOWED_PATCH_PREFIXES):
            raise HarnessError(f"patch path is outside the allowed product surface: {path}")
    return sorted(paths)


def validate_delivery(candidate_dir: Path) -> tuple[dict[str, Any], str, list[str]]:
    if not candidate_dir.is_dir():
        raise HarnessError(f"candidate delivery is not a directory: {candidate_dir}")
    entries = {entry.name for entry in candidate_dir.iterdir()}
    if entries != REQUIRED_DELIVERY:
        raise HarnessError(
            f"delivery must contain exactly {sorted(REQUIRED_DELIVERY)}; found {sorted(entries)}"
        )
    try:
        patch_text = (candidate_dir / "solution.patch").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"solution.patch is not readable UTF-8: {exc}") from exc
    if not patch_text.strip():
        raise HarnessError("solution.patch is empty")
    patch_paths = parse_patch_paths(patch_text)
    edit = load_json(candidate_dir / "edit_report.json")
    run = load_json(candidate_dir / "run_report.json")
    required_edit = {
        "schema_version",
        "feature_summary",
        "changed_paths",
        "commands",
        "compatibility_notes",
        "limitations",
    }
    missing_edit = required_edit - set(edit)
    if missing_edit:
        raise HarnessError(f"edit_report.json missing fields: {sorted(missing_edit)}")
    if edit.get("schema_version") != "1.0" or not isinstance(edit.get("feature_summary"), str) or not edit.get("feature_summary", "").strip():
        raise HarnessError("edit_report requires schema_version 1.0 and a nonempty feature_summary")
    reported_paths = edit.get("changed_paths")
    if not isinstance(reported_paths, list) or sorted(reported_paths) != patch_paths:
        raise HarnessError("edit_report changed_paths must exactly equal patch paths")
    if not isinstance(edit.get("commands"), list):
        raise HarnessError("edit_report commands must be an array")
    for item in edit.get("commands", []):
        if not isinstance(item, dict) or not {"command", "exit_code", "result"} <= set(item):
            raise HarnessError("each edit_report command needs command, exit_code, and result")
    for key in ("compatibility_notes", "limitations"):
        if not isinstance(edit.get(key), list) or any(not isinstance(item, str) for item in edit[key]):
            raise HarnessError(f"edit_report {key} must be a string array")
    required_run = {
        "schema_version",
        "status",
        "artifact_paths",
        "errors",
        "runtime_seconds",
        "peak_memory_bytes",
        "api_calls",
    }
    missing_run = required_run - set(run)
    if missing_run:
        raise HarnessError(f"run_report.json missing fields: {sorted(missing_run)}")
    if set(run) != required_run:
        raise HarnessError(
            "run_report.json must contain exactly the shared schema fields; "
            f"found extras={sorted(set(run) - required_run)}"
        )
    if run.get("schema_version") != "1.0":
        raise HarnessError("run_report schema_version must be 1.0")
    if not isinstance(run.get("status"), str) or not run["status"].strip():
        raise HarnessError("run_report status must be a nonempty string")
    artifact_paths = run.get("artifact_paths")
    if (
        not isinstance(artifact_paths, list)
        or any(not isinstance(item, str) for item in artifact_paths)
        or set(artifact_paths) != REQUIRED_DELIVERY
        or len(artifact_paths) != 3
    ):
        raise HarnessError("run_report artifact_paths must list exactly the three delivery files")
    if not isinstance(run.get("errors"), list) or any(not isinstance(item, str) for item in run["errors"]):
        raise HarnessError("run_report errors must be a string array")
    runtime = run.get("runtime_seconds")
    if not isinstance(runtime, (int, float)) or isinstance(runtime, bool) or runtime < 0:
        raise HarnessError("run_report runtime_seconds must be a nonnegative number")
    peak = run.get("peak_memory_bytes")
    if not isinstance(peak, int) or isinstance(peak, bool) or peak < 0:
        raise HarnessError("run_report peak_memory_bytes must be a nonnegative integer")
    usage = run.get("api_calls")
    if not isinstance(usage, dict) or set(usage) != {"gateway", "serper", "web_retrieval"}:
        raise HarnessError("run_report api_calls must contain exactly gateway, serper, and web_retrieval")
    for key in ("gateway", "serper", "web_retrieval"):
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HarnessError(f"run_report api_calls.{key} must be a nonnegative integer")
    return {"edit_report": edit, "run_report": run}, patch_text, patch_paths


def git(repo: Path, args: list[str], *, check: bool = True) -> CommandResult:
    result = run_limited(["git", *args], repo, timeout=120)
    if check and result.exit_code != 0:
        raise HarnessError(f"git {' '.join(args)} failed: {result.stderr[-1000:]}")
    return result


def prepare_candidate(
    benchmark_root: Path,
    candidate_dir: Path,
    work_dir: Path,
    *,
    install: bool = True,
) -> PreparedCandidate:
    pinned = benchmark_root / "input" / "repository"
    observed_hash = tree_hash(pinned)
    if observed_hash != PINNED_SOURCE_TREE_HASH:
        raise HarnessError(
            "pinned input/repository changed: "
            f"expected {PINNED_SOURCE_TREE_HASH}, observed {observed_hash}"
        )
    delivery, patch_text, patch_paths = validate_delivery(candidate_dir)
    source_dir = work_dir / "patched-openwiki"
    if source_dir.exists():
        shutil.rmtree(source_dir)
    shutil.copytree(benchmark_root / "input" / "repository", source_dir)
    git(source_dir, ["init", "-q"])
    git(source_dir, ["config", "user.email", "benchmark@example.invalid"])
    git(source_dir, ["config", "user.name", "OpenWiki Benchmark"])
    git(source_dir, ["add", "."])
    git(source_dir, ["commit", "-qm", "pinned source"])
    patch_file = candidate_dir / "solution.patch"
    first_check = git(source_dir, ["apply", "--check", str(patch_file)], check=False)
    if first_check.exit_code != 0:
        raise HarnessError(f"solution.patch does not apply: {first_check.stderr[-2000:]}")
    applied = git(source_dir, ["apply", str(patch_file)], check=False)
    if applied.exit_code != 0:
        raise HarnessError(f"solution.patch application failed: {applied.stderr[-2000:]}")
    second_check = git(source_dir, ["apply", "--check", str(patch_file)], check=False)
    if second_check.exit_code == 0:
        raise HarnessError("solution.patch is replay-applicable; it must apply exactly once")
    setup_results: list[CommandResult] = []
    if install:
        setup_results.extend(install_and_build(source_dir))
    return PreparedCandidate(source_dir, patch_paths, setup_results, delivery)


def install_and_build(source_dir: Path) -> list[CommandResult]:
    results: list[CommandResult] = []
    external_modules = os.environ.get("OPENWIKI_NODE_MODULES")
    modules = source_dir / "node_modules"
    if external_modules and not modules.exists():
        target = Path(external_modules).resolve()
        if not target.is_dir():
            raise HarnessError(f"OPENWIKI_NODE_MODULES is not a directory: {target}")
        modules.symlink_to(target, target_is_directory=True)
    if not modules.exists():
        pnpm = shutil.which("pnpm")
        package_manager = [pnpm] if pnpm else ["npm", "exec", "--yes", "pnpm@10.33.2", "--"]
        install = run_limited(
            [
                *package_manager,
                "install",
                "--frozen-lockfile",
                "--ignore-scripts",
                "--prefer-offline",
            ],
            source_dir,
            timeout=600,
        )
        results.append(install)
        if install.exit_code != 0:
            raise HarnessError(f"dependency installation failed: {install.stderr[-2000:]}")
    build_commands = [
        ["node", "node_modules/typescript/bin/tsc", "-p", "tsconfig.json"],
        ["node", "node_modules/typescript/bin/tsc", "-p", "tsconfig.client.json"],
        [
            "node",
            "node_modules/vitest/vitest.mjs",
            "run",
            "test/commands.test.ts",
            "test/update-noop.test.ts",
            "test/docs-only-backend.test.ts",
            "test/frontmatter-validator.test.ts",
            "test/okf/frontmatter.test.ts",
            "test/index-middleware.test.ts",
            "test/wiki-link-validator.test.ts",
            "test/mermaid-wiki.test.ts",
        ],
    ]
    for argv in build_commands:
        result = run_limited(argv, source_dir, timeout=600)
        results.append(result)
        if result.exit_code != 0:
            raise HarnessError(f"build/regression command failed: {' '.join(argv)}\n{result.stderr[-3000:]}")
    return results


def overlay_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Do not preserve overlay mtimes: equal-size synthetic edits can
            # otherwise satisfy Git's stat-cache shortcut and disappear.
            shutil.copyfile(item, target)


def materialize_case(
    case_dir: Path,
    case_work_dir: Path,
    spec_override: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any], Path]:
    if spec_override is not None:
        spec = json.loads(json.dumps(spec_override))
    else:
        public_specs = load_json(case_dir.parent / "public_specs.json")
        candidate = public_specs.get(case_dir.name)
        if not isinstance(candidate, dict):
            raise HarnessError(f"missing public runner spec for {case_dir.name}")
        spec = candidate
    project = case_dir / "assets" / "project"
    before_assets = project / "before"
    after_overlay = project / "after"
    repo = case_work_dir / "repository"
    if case_work_dir.exists():
        shutil.rmtree(case_work_dir)
    repo.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(before_assets, repo)
    before_snapshot = case_work_dir / "before-snapshot"
    shutil.copytree(before_assets, before_snapshot)
    git(repo, ["init", "-q"])
    git(repo, ["config", "user.email", "fixture@example.invalid"])
    git(repo, ["config", "user.name", "Synthetic Fixture"])
    git(repo, ["add", "."])
    git(repo, ["commit", "-qm", "before change"])
    overlay_tree(after_overlay, repo)
    for raw in spec.get("delete_paths", []):
        relative = safe_relative_path(str(raw))
        target = repo / relative
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    git(repo, ["add", "-A"])
    git(repo, ["commit", "-qm", "after change"])
    after_snapshot = case_work_dir / "after-snapshot"
    shutil.copytree(repo, after_snapshot, ignore=shutil.ignore_patterns(".git"))
    kind = spec["change_kind"]
    if kind == "git":
        change_source: dict[str, Any] = {"kind": "git", "base": "HEAD~1", "head": "HEAD"}
    elif kind == "diff":
        diff_result = git(repo, ["diff", "HEAD~1", "HEAD", "--binary"])
        (repo / "change.diff").write_text(diff_result.stdout, encoding="utf-8")
        change_source = {"kind": "diff", "path": "change.diff"}
    elif kind == "directories":
        change_source = {
            "kind": "directories",
            "before": str(before_snapshot.resolve()),
            "after": str(after_snapshot.resolve()),
        }
    else:
        raise HarnessError(f"unsupported fixture change kind: {kind}")
    manifest = {
        "schema_version": "1.0",
        "change_source": change_source,
        "docs_root": "openwiki",
        "report_path": "impact-report.json",
        "diff_path": "documentation.diff",
        "example_timeout_ms": 5000,
        "future_compatible_field": {"ignored": True},
    }
    transaction = spec.get("transaction")
    if isinstance(transaction, dict):
        manifest["transaction"] = transaction
    publication = spec.get("publication")
    if isinstance(publication, dict):
        manifest["publication"] = publication
    search = spec.get("search")
    if isinstance(search, dict):
        manifest["search"] = search
    (repo / "impact-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    preseed_transaction_state(repo, spec)
    preseed_surface_state(repo, spec)
    if spec.get("security_case"):
        (case_work_dir / "parent-secret.txt").write_text(
            "PARENT-CANARY-MUST-NOT-BE-READ-OR-CHANGED\n", encoding="utf-8"
        )
    return repo, spec, before_snapshot


def product_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.endswith("_API_KEY") or upper.endswith("_TOKEN") or upper in {
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }:
            env.pop(key, None)
    env.update(
        {
            "CI": "1",
            "OPENWIKI_TELEMETRY": "0",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "localhost,127.0.0.1",
        }
    )
    return env


def transaction_paths(repo: Path, transaction: dict[str, Any]) -> dict[str, Path]:
    state_dir = repo / safe_relative_path(str(transaction["state_dir"]))
    tenant = safe_relative_path(str(transaction["tenant_id"]))
    request = safe_relative_path(str(transaction["request_id"]))
    tenant_dir = state_dir / "tenants" / tenant
    return {
        "root": state_dir,
        "tenant": tenant_dir,
        "state": tenant_dir / "state.json",
        "claim": tenant_dir / "claims" / f"{request}.json",
        "receipt": tenant_dir / "receipts" / f"{request}.json",
        "staging": tenant_dir / "staging" / request,
        "lock": tenant_dir / "write.lock",
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def preseed_transaction_state(repo: Path, spec: dict[str, Any]) -> None:
    mode = spec.get("preseed")
    transaction = spec.get("transaction")
    if not mode or not isinstance(transaction, dict):
        return
    paths = transaction_paths(repo, transaction)
    generation = int(transaction["generation"])
    prior_generation = max(0, generation - 1)
    write_json(
        paths["state"],
        {
            "schema_version": "1.0",
            "tenant_id": transaction["tenant_id"],
            "latest_generation": prior_generation,
            "latest_request_id": "prior-request" if generation > 0 else "",
        },
    )
    phase = "prepared" if mode == "expired_prepared_corrupt_stage" else "claimed"
    write_json(
        paths["claim"],
        {
            "schema_version": "1.0",
            "tenant_id": transaction["tenant_id"],
            "request_id": transaction["request_id"],
            "generation": generation,
            "payload_digest": transaction["payload_digest"],
            "owner_id": "terminated-owner",
            "phase": phase,
            "lease_expires_at": "2000-01-01T00:00:00.000Z",
        },
    )
    staged = paths["staging"] / "openwiki" / "incomplete.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("TRUNCATED-UNTRUSTED-STAGING\n", encoding="utf-8")


def publication_paths(repo: Path, publication: dict[str, Any]) -> dict[str, Path]:
    root = repo / safe_relative_path(str(publication["root"]))
    site = root / "sites" / safe_relative_path(str(publication["site_id"]))
    release = site / "releases" / str(publication["generation"])
    return {
        "root": root,
        "site": site,
        "active": site / "active.json",
        "release": release,
        "manifest": release / "manifest.json",
        "index": release / "index.html",
        "graph": release / "graph.json",
        "pages": release / "pages",
    }


def search_paths(repo: Path, search: dict[str, Any]) -> dict[str, Path]:
    root = repo / safe_relative_path(str(search["root"]))
    index_root = root / "indexes" / safe_relative_path(str(search["index_id"]))
    generation = index_root / "generations" / str(search["generation"])
    return {
        "root": root,
        "index_root": index_root,
        "active": index_root / "active.json",
        "generation": generation,
        "manifest": generation / "manifest.json",
        "data": generation / "index.json",
    }


def preseed_surface_state(repo: Path, spec: dict[str, Any]) -> None:
    if spec.get("preseed_surfaces") != "partial_generation":
        return
    publication = spec.get("publication")
    search = spec.get("search")
    if isinstance(publication, dict):
        paths = publication_paths(repo, publication)
        paths["release"].mkdir(parents=True, exist_ok=True)
        (paths["release"] / ".partial-untrusted").write_text(
            "not a complete release\n", encoding="utf-8"
        )
    if isinstance(search, dict):
        paths = search_paths(repo, search)
        paths["generation"].mkdir(parents=True, exist_ok=True)
        (paths["generation"] / ".partial-untrusted").write_text(
            '{"documents":[', encoding="utf-8"
        )


def update_manifest(repo: Path, name: str, transform: Any) -> dict[str, Any]:
    manifest = load_json(repo / "impact-manifest.json")
    transform(manifest)
    write_json(repo / name, manifest)
    return manifest


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_tree_hash(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def invoke_product(
    source_dir: Path,
    repo: Path,
    manifest_path: str = "impact-manifest.json",
) -> CommandResult:
    return run_limited(
        [
            "node",
            str(source_dir / "dist" / "cli.js"),
            "--update",
            "--impact-manifest",
            manifest_path,
            "--print",
        ],
        repo,
        timeout=TIMEOUT_SECONDS,
        env=product_environment(),
    )


def invoke_search(
    source_dir: Path,
    repo: Path,
    search: dict[str, Any],
    query: str,
    limit: int,
) -> CommandResult:
    return run_limited(
        [
            "node",
            str(source_dir / "dist" / "cli.js"),
            "search",
            "--index-root",
            str(search["root"]),
            "--index-id",
            str(search["index_id"]),
            "--query",
            query,
            "--limit",
            str(limit),
            "--json",
        ],
        repo,
        timeout=120,
        env=product_environment(),
    )


def file_hashes(root: Path, relative_root: str = "openwiki") -> dict[str, str]:
    result: dict[str, str] = {}
    base = root / relative_root
    if not base.exists():
        return result
    for path in sorted(base.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def expected_corpus_hashes(repo: Path) -> dict[str, str]:
    excluded = {"INSTRUCTIONS.md", "_plan.md", "log.md"}
    result: dict[str, str] = {}
    root = repo / "openwiki"
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*.md")):
        if path.name in excluded or path.is_symlink() or not path.is_file():
            continue
        result[path.relative_to(repo).as_posix()] = sha256_file(path)
    return result


def parse_single_json_stdout(result: CommandResult) -> dict[str, Any] | None:
    if result.exit_code != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _identity_matches(
    value: dict[str, Any], kind_id: str, config: dict[str, Any], digest: str
) -> bool:
    return (
        value.get("schema_version") == "1.0"
        and value.get(kind_id) == config[kind_id]
        and value.get("generation") == config["generation"]
        and value.get("payload_digest") == digest
    )


def validate_publication_surface(
    repo: Path, spec: dict[str, Any], report: dict[str, Any]
) -> list[tuple[str, bool, str]]:
    publication = spec.get("publication")
    transaction = spec.get("transaction")
    if not isinstance(publication, dict) or not isinstance(transaction, dict):
        return [("PUBLICATION-SELECTED", False, "case requires publication and transaction")]
    digest = str(transaction["payload_digest"])
    paths = publication_paths(repo, publication)
    checks: list[tuple[str, bool, str]] = []
    try:
        active = load_json(paths["active"])
        manifest = load_json(paths["manifest"])
    except HarnessError as exc:
        return [("PUBLICATION-AVAILABLE", False, str(exc))]
    release_rel = paths["release"].relative_to(repo).as_posix()
    identity_ok = (
        _identity_matches(active, "site_id", publication, digest)
        and _identity_matches(manifest, "site_id", publication, digest)
        and active.get("release_path") == release_rel
        and active.get("manifest_sha256") == sha256_file(paths["manifest"])
        and manifest.get("base_path") == publication["base_path"]
    )
    checks.append(("PUBLICATION-IDENTITY", identity_ok, f"active={active} release={release_rel}"))

    expected_docs = expected_corpus_hashes(repo)
    documented = manifest.get("documentation_hashes")
    corpus_ok = (
        isinstance(documented, dict)
        and list(documented) == sorted(documented)
        and documented == expected_docs
    )
    checks.append(("PUBLICATION-CORPUS", corpus_ok, f"expected={sorted(expected_docs)} actual={sorted(documented) if isinstance(documented, dict) else type(documented).__name__}"))

    file_map = manifest.get("files")
    actual_files = {
        path.relative_to(paths["release"]).as_posix(): sha256_file(path)
        for path in sorted(paths["release"].rglob("*"))
        if path.is_file() and path != paths["manifest"]
    }
    hashes_ok = (
        isinstance(file_map, dict)
        and list(file_map) == sorted(file_map)
        and file_map == actual_files
        and ".partial-untrusted" not in actual_files
    )
    checks.append(("PUBLICATION-FILE-HASHES", hashes_ok, f"declared={len(file_map) if isinstance(file_map, dict) else -1} actual={len(actual_files)}"))

    page_ok = True
    rendered_text = ""
    for doc_path in expected_docs:
        relative = PurePosixPath(doc_path).relative_to("openwiki").with_suffix(".html")
        rendered = paths["pages"] / relative
        if not rendered.is_file():
            page_ok = False
            continue
        value = rendered.read_text(encoding="utf-8", errors="replace")
        rendered_text += value
        source = (repo / doc_path).read_text(encoding="utf-8")
        headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", source, re.MULTILINE)
        if headings and not any(re.sub(r"[`*_]", "", heading) in value for heading in headings):
            page_ok = False
    index_text = paths["index"].read_text(encoding="utf-8", errors="replace") if paths["index"].is_file() else ""
    checks.append(("PUBLICATION-PAGES", page_ok and bool(index_text), f"rendered_pages={len(list(paths['pages'].rglob('*.html'))) if paths['pages'].is_dir() else 0}"))

    try:
        graph = load_json(paths["graph"])
    except HarnessError:
        graph = {}
    nodes = graph.get("nodes")
    node_paths = {
        item.get("path") for item in nodes if isinstance(item, dict)
    } if isinstance(nodes, list) else set()
    graph_ok = node_paths == set(expected_docs) and isinstance(graph.get("edges"), list)
    checks.append(("PUBLICATION-GRAPH", graph_ok, f"nodes={sorted(str(item) for item in node_paths)}"))

    all_html = index_text + rendered_text
    unsafe = re.search(
        r"(?is)<script\b|\son[a-z]+\s*=|javascript:|https?://|//cdn\.|file:|\x1b",
        all_html,
    )
    report_value = report.get("publication")
    report_ok = (
        isinstance(report_value, dict)
        and _identity_matches(report_value, "site_id", publication, digest)
        and report_value.get("status") in {"published", "unchanged"}
        and report_value.get("manifest_sha256") == sha256_file(paths["manifest"])
        and report_value.get("page_count") == len(expected_docs)
    )
    checks.append(("PUBLICATION-OFFLINE-REPORT", unsafe is None and report_ok, f"unsafe={unsafe.group(0) if unsafe else None} report={report_value}"))
    return checks


def validate_search_surface(
    source_dir: Path,
    repo: Path,
    spec: dict[str, Any],
    report: dict[str, Any],
) -> tuple[list[tuple[str, bool, str]], list[CommandResult]]:
    search = spec.get("search")
    transaction = spec.get("transaction")
    if not isinstance(search, dict) or not isinstance(transaction, dict):
        return [("SEARCH-SELECTED", False, "case requires search and transaction")], []
    digest = str(transaction["payload_digest"])
    paths = search_paths(repo, search)
    checks: list[tuple[str, bool, str]] = []
    invocations: list[CommandResult] = []
    try:
        active = load_json(paths["active"])
        manifest = load_json(paths["manifest"])
        data = load_json(paths["data"])
    except HarnessError as exc:
        return [("SEARCH-AVAILABLE", False, str(exc))], invocations
    generation_rel = paths["generation"].relative_to(repo).as_posix()
    identity_ok = (
        _identity_matches(active, "index_id", search, digest)
        and _identity_matches(manifest, "index_id", search, digest)
        and _identity_matches(data, "index_id", search, digest)
        and active.get("generation_path") == generation_rel
        and active.get("manifest_sha256") == sha256_file(paths["manifest"])
        and manifest.get("index_sha256") == sha256_file(paths["data"])
    )
    checks.append(("SEARCH-IDENTITY", identity_ok, f"active={active} generation={generation_rel}"))

    expected_docs = expected_corpus_hashes(repo)
    documented = manifest.get("documentation_hashes")
    documents = data.get("documents")
    doc_paths = [item.get("path") for item in documents if isinstance(item, dict)] if isinstance(documents, list) else []
    corpus_ok = (
        isinstance(documented, dict)
        and list(documented) == sorted(documented)
        and documented == expected_docs
        and doc_paths == sorted(expected_docs)
        and len(doc_paths) == len(set(doc_paths))
        and ".partial-untrusted" not in {p.name for p in paths["generation"].rglob("*")}
    )
    checks.append(("SEARCH-CORPUS", corpus_ok, f"expected={sorted(expected_docs)} actual={doc_paths}"))

    records_ok = isinstance(documents, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("title"), str)
        and bool(item.get("title", "").strip())
        and isinstance(item.get("headings"), list)
        and isinstance(item.get("text"), str)
        and item.get("documentation_sha256") == expected_docs.get(str(item.get("path")))
        for item in documents
    )
    serialized_data = paths["data"].read_text(encoding="utf-8", errors="replace")
    reserved_absent = all(name not in doc_paths for name in ("openwiki/INSTRUCTIONS.md", "openwiki/_plan.md", "openwiki/log.md"))
    checks.append(("SEARCH-RECORDS", records_ok and reserved_absent and "openwiki:example" not in serialized_data, f"records={len(documents) if isinstance(documents, list) else -1}"))

    queries_ok = True
    result_details: list[str] = []
    state_before = state_tree_hash(paths["root"])
    for query_spec in spec.get("search_queries", []):
        query = str(query_spec["query"])
        limit = int(query_spec.get("limit", 10))
        invocation = invoke_search(source_dir, repo, search, query, limit)
        invocations.append(invocation)
        payload = parse_single_json_stdout(invocation)
        results = payload.get("results") if isinstance(payload, dict) else None
        actual_paths = [item.get("path") for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        required_paths = list(query_spec.get("expected_paths", []))
        forbidden_paths = set(query_spec.get("forbidden_paths", []))
        one_ok = (
            isinstance(payload, dict)
            and _identity_matches(payload, "index_id", search, digest)
            and payload.get("query") == query
            and isinstance(results, list)
            and len(results) <= limit
            and all(path in actual_paths for path in required_paths)
            and not (forbidden_paths & set(actual_paths))
            and all(
                isinstance(item.get("anchor"), str)
                and isinstance(item.get("snippet"), str)
                and bool(item.get("snippet", "").strip())
                and isinstance(item.get("score"), (int, float))
                and not isinstance(item.get("score"), bool)
                and 0 <= float(item["score"]) < float("inf")
                for item in results
                if isinstance(item, dict)
            )
        )
        queries_ok = queries_ok and one_ok
        result_details.append(f"{query!r}: exit={invocation.exit_code} paths={actual_paths}")
    checks.append(("SEARCH-QUERIES", queries_ok and bool(spec.get("search_queries")), "; ".join(result_details)))
    checks.append(("SEARCH-READ-ONLY", state_tree_hash(paths["root"]) == state_before, "queries changed no search bytes"))

    report_value = report.get("search_index")
    report_ok = (
        isinstance(report_value, dict)
        and _identity_matches(report_value, "index_id", search, digest)
        and report_value.get("status") in {"indexed", "unchanged"}
        and report_value.get("manifest_sha256") == sha256_file(paths["manifest"])
        and report_value.get("document_count") == len(expected_docs)
    )
    checks.append(("SEARCH-REPORT", report_ok, f"report={report_value}"))
    return checks, invocations


def parse_impact_report(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        report = load_json(path)
    except HarnessError as exc:
        return None, [str(exc)]
    required = {"schema_version", "status", "change_source", "affected", "changed_paths", "validation", "security", "errors"}
    missing = required - set(report)
    if missing:
        errors.append(f"missing top-level fields {sorted(missing)}")
    if report.get("schema_version") != "1.0":
        errors.append("schema_version must be 1.0")
    if report.get("status") not in {"updated", "no_changes", "partial", "failed"}:
        errors.append("invalid status")
    change_source = report.get("change_source")
    if not isinstance(change_source, dict) or change_source.get("kind") not in {"git", "diff", "directories"}:
        errors.append("change_source.kind is invalid")
    affected = report.get("affected")
    required_affected = {"pages", "examples", "diagrams", "links", "stale_claims"}
    if not isinstance(affected, dict) or not required_affected <= set(affected):
        errors.append("affected object is incomplete")
    else:
        for key in required_affected:
            if not isinstance(affected.get(key), list):
                errors.append(f"affected.{key} must be an array")
    changed = report.get("changed_paths")
    if not isinstance(changed, list) or any(not isinstance(item, str) for item in changed):
        errors.append("changed_paths must be a string array")
    elif changed != sorted(set(changed)):
        errors.append("changed_paths must be deduplicated and sorted")
    validation = report.get("validation")
    validation_fields = {
        "examples_total",
        "examples_passed",
        "examples_failed",
        "links_broken",
        "schema_valid",
    }
    if not isinstance(validation, dict) or not validation_fields <= set(validation):
        errors.append("validation object is incomplete")
    elif (
        any(
            not isinstance(validation.get(key), int)
            or isinstance(validation.get(key), bool)
            or validation.get(key, -1) < 0
            for key in validation_fields - {"schema_valid"}
        )
        or not isinstance(validation.get("schema_valid"), bool)
    ):
        errors.append("validation counters/types are invalid")
    security = report.get("security")
    if not isinstance(security, dict) or any(
        not isinstance(security.get(key), int)
        or isinstance(security.get(key), bool)
        or security.get(key, -1) < 0
        for key in ("ignored_instruction_count", "unsafe_execution_count")
    ):
        errors.append("security counters are invalid")
    if not isinstance(report.get("errors"), list) or any(
        not isinstance(item, str) for item in report.get("errors", [])
    ):
        errors.append("errors must be a string array")
    return report, errors


def report_page_paths(report: dict[str, Any]) -> set[str]:
    pages = report.get("affected", {}).get("pages", [])
    result: set[str] = set()
    for item in pages:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            result.add(item["path"])
    return result


def report_example_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    examples = report.get("affected", {}).get("examples", [])
    return {
        item["id"]: item
        for item in examples
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def changed_doc_paths(repo: Path) -> set[str]:
    result = git(repo, ["diff", "--name-only", "HEAD", "--", "openwiki"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def canonical_doc_diff(repo: Path) -> str:
    return git(repo, ["diff", "--no-ext-diff", "--", "openwiki"]).stdout


def validate_documentation_diff(repo: Path, diff_path: Path) -> tuple[bool, str]:
    try:
        text = diff_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"diff is not readable UTF-8: {exc}"
    if not text:
        return True, "empty diff"
    headers = re.findall(r"^diff --git a/(.+?) b/(.+?)$", text, re.MULTILINE)
    if not headers:
        return False, "nonempty diff has no Git file headers"
    for old, new in headers:
        try:
            old_path = safe_relative_path(old)
            new_path = safe_relative_path(new)
        except HarnessError as exc:
            return False, str(exc)
        if not old_path.startswith("openwiki/") or not new_path.startswith("openwiki/"):
            return False, f"non-documentation path in diff: {old_path} -> {new_path}"
    reverse_check = git(repo, ["apply", "--check", "--reverse", str(diff_path)], check=False)
    if reverse_check.exit_code != 0:
        return False, f"diff cannot reverse-apply to final docs: {reverse_check.stderr[-500:]}"
    return True, "unified diff reverse-applies to final documentation"


def strip_owned_content(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            text = "<OPENWIKI_FRONTMATTER>\n" + text[end + 5 :]
    return GENERATED_RE.sub(lambda match: f"{match.group(1)}<OWNED:{match.group(2)}>{match.group(3)}", text)


def validate_frontmatter(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if path.name in {"index.md", "log.md", "INSTRUCTIONS.md"}:
        return True
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---\n", 4)
    if end < 0:
        return False
    return bool(re.search(r"^type:\s*\S", text[4:end], re.MULTILINE))


def github_slug(heading: str) -> str:
    value = heading.strip().lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return value.replace(" ", "-")


def validate_internal_links(repo: Path) -> list[str]:
    issues: list[str] = []
    docs = repo / "openwiki"
    for page in sorted(docs.rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        for href in LINK_RE.findall(text):
            if href.startswith(("http://", "https://", "mailto:", "#")):
                continue
            raw_target, _, anchor = href.partition("#")
            if raw_target.startswith("/"):
                target = repo / raw_target.lstrip("/")
            else:
                target = (page.parent / raw_target).resolve()
            try:
                target.relative_to(repo.resolve())
            except ValueError:
                issues.append(f"{page.relative_to(repo)}: escaping link {href}")
                continue
            if not target.exists():
                issues.append(f"{page.relative_to(repo)}: missing target {href}")
                continue
            if anchor and target.suffix.lower() == ".md":
                target_text = target.read_text(encoding="utf-8")
                anchors = set(re.findall(r"<a\s+id=[\"']([^\"']+)[\"']", target_text, re.IGNORECASE))
                counts: dict[str, int] = {}
                for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", target_text, re.MULTILINE):
                    base = github_slug(heading)
                    count = counts.get(base, 0)
                    anchors.add(base if count == 0 else f"{base}-{count}")
                    counts[base] = count + 1
                if anchor not in anchors:
                    issues.append(f"{page.relative_to(repo)}: missing anchor {href}")
    return issues


def validate_mermaid(repo: Path) -> list[str]:
    issues: list[str] = []
    for page in sorted((repo / "openwiki").rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        for index, content in enumerate(re.findall(r"```mermaid\n(.*?)\n```", text, re.DOTALL), 1):
            first = content.splitlines()[0].strip() if content.splitlines() else ""
            if first not in {"flowchart LR", "flowchart TD", "sequenceDiagram", "stateDiagram-v2", "erDiagram"}:
                issues.append(f"{page.relative_to(repo)} fence {index}: unsupported or malformed header")
            if re.search(r"\bend\s*\[", content):
                issues.append(f"{page.relative_to(repo)} fence {index}: reserved end node")
    return issues


def extract_examples(repo: Path) -> dict[str, tuple[dict[str, Any], str, str]]:
    examples: dict[str, tuple[dict[str, Any], str, str]] = {}
    for page in sorted((repo / "openwiki").rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        for raw_metadata, code in EXAMPLE_RE.findall(text):
            try:
                metadata = json.loads(raw_metadata)
            except json.JSONDecodeError:
                continue
            example_id = metadata.get("id")
            if isinstance(example_id, str):
                examples[example_id] = (metadata, code, page.relative_to(repo).as_posix())
    return examples


def independently_run_examples(
    repo: Path, expected: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], bool]:
    extracted = extract_examples(repo)
    results: dict[str, dict[str, Any]] = {}
    target_hash_before = tree_hash(repo)
    for example_id, expected_stdout in expected.items():
        if example_id not in extracted:
            results[example_id] = {"passed": False, "detail": "example metadata/fence missing"}
            continue
        metadata, code, page_path = extracted[example_id]
        if expected_stdout is None:
            passed = metadata.get("complete") is False
            results[example_id] = {"passed": passed, "detail": "incomplete example skipped" if passed else "incomplete example not marked complete:false"}
            continue
        command = metadata.get("command")
        file_name = metadata.get("file")
        if (
            metadata.get("complete") is not True
            or not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) for part in command)
            or not isinstance(file_name, str)
        ):
            results[example_id] = {"passed": False, "detail": "invalid executable metadata"}
            continue
        with tempfile.TemporaryDirectory(prefix=f"openwiki-example-{example_id}-") as temp:
            sandbox = Path(temp) / "repository"
            shutil.copytree(repo, sandbox, ignore=shutil.ignore_patterns(".git", "impact-report.json", "documentation.diff"))
            page_dir = sandbox / Path(page_path).parent
            extracted_file = page_dir / safe_relative_path(file_name)
            extracted_file.parent.mkdir(parents=True, exist_ok=True)
            extracted_file.write_text(code + "\n", encoding="utf-8")
            run = run_limited(command, sandbox, timeout=10, env=product_environment())
            passed = run.exit_code == 0 and run.stdout == expected_stdout
            results[example_id] = {
                "passed": passed,
                "detail": f"exit={run.exit_code} expected={expected_stdout!r} actual={run.stdout!r}",
                "stdout": run.stdout,
                "exit_code": run.exit_code,
            }
    return results, tree_hash(repo) == target_hash_before


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"impact-report.json", "documentation.diff"}:
            continue
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def protected_repo_hashes(repo: Path) -> dict[str, str]:
    """Hash target files that impact mode is never authorized to change."""
    result: dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(repo).as_posix()
        if (
            relative.startswith("openwiki/")
            or relative.startswith(".openwiki-impact/")
            or relative.startswith((".openwiki-public/", ".openwiki-search/", ".reader-output/", ".reader-index/", ".published-wiki/", ".search-wiki/", ".runtime-site/", ".runtime-search/", ".monorepo-site/", ".monorepo-index/", ".secure-site/", ".secure-index/"))
            or relative.startswith("impact-report")
            or relative.startswith("documentation")
            or relative.startswith("impact-manifest")
        ):
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def external_case_hashes(case_work_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    repo = case_work_dir / "repository"
    for path in sorted(case_work_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            path.relative_to(repo)
            continue
        except ValueError:
            pass
        relative = path.relative_to(case_work_dir).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def stale_kind_matches(actual: str, expected: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", actual.lower()).strip("-")
    aliases = {
        "symbol-rename": ("rename", "renamed"),
        "default-behavior": ("default", "behavior"),
        "module-move": ("move", "module", "relocat"),
        "compatibility-alias": ("compat", "alias", "re-export"),
        "return-type": ("type", "representation"),
        "transitive-claim": ("transitive", "dependent"),
        "removed-api": ("remov", "delet"),
        "deprecated-api": ("deprecat",),
        "migration-guidance": ("migration", "migrate"),
        "diagram-edge": ("diagram", "mermaid", "edge"),
        "broken-link": ("link", "anchor"),
        "example-output": ("example", "output", "stdout"),
    }
    return any(part in normalized for part in aliases.get(expected, (expected,)))


def add_assert(assertions: list[Assertion], assertion_id: str, passed: bool, detail: str) -> None:
    assertions.append(Assertion(assertion_id, passed, detail))


def proportional_set_score(expected: set[str], actual: set[str], weight: float) -> float:
    if not expected:
        return weight if not actual else 0.0
    recall = len(expected & actual) / len(expected)
    precision = len(expected & actual) / len(actual) if actual else 0.0
    return weight * (recall + precision) / 2.0


def _evaluate_legacy_case(
    prepared: PreparedCandidate,
    case_dir: Path,
    case_work_dir: Path,
    *,
    keep_work: bool = False,
) -> CaseResult:
    repo, spec, before_snapshot = materialize_case(case_dir, case_work_dir)
    case_id = str(spec["case_id"])
    before_docs = file_hashes(repo)
    protected_before = protected_repo_hashes(repo)
    external_before = external_case_hashes(case_work_dir)
    invocation = invoke_product(prepared.source_dir, repo)
    assertions: list[Assertion] = []
    if invocation.exit_code != 0:
        add_assert(assertions, "VALID-CLI", False, f"CLI exit {invocation.exit_code}: {invocation.stderr[-1000:]}")
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, worktree=str(repo) if keep_work else None)
    add_assert(assertions, "VALID-CLI", True, "real openwiki --update impact invocation exited 0")
    report, report_errors = parse_impact_report(repo / "impact-report.json")
    if report is None:
        add_assert(assertions, "VALID-REPORT", False, "; ".join(report_errors))
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, worktree=str(repo) if keep_work else None)
    add_assert(assertions, "VALID-REPORT", not report_errors, "; ".join(report_errors) or "impact report schema valid")
    if report_errors:
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, worktree=str(repo) if keep_work else None)
    diff_path = repo / "documentation.diff"
    if not diff_path.is_file():
        add_assert(assertions, "VALID-DIFF", False, "documentation.diff missing")
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, worktree=str(repo) if keep_work else None)
    diff_valid, diff_valid_detail = validate_documentation_diff(repo, diff_path)
    add_assert(assertions, "VALID-DIFF", diff_valid, diff_valid_detail)
    if not diff_valid:
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, worktree=str(repo) if keep_work else None)

    expected_pages = set(spec["expected_pages"])
    direct_pages = set(spec["direct_pages"])
    unaffected_pages = set(spec["unaffected_pages"])
    reported_pages = report_page_paths(report)
    changed_pages = changed_doc_paths(repo)
    report_changed = set(report.get("changed_paths", []))
    expected_no_changes = bool(spec.get("expect_no_changes"))

    impact_score = proportional_set_score(expected_pages, reported_pages, 16.0)
    reported_direct = {
        item["path"]
        for item in report["affected"]["pages"]
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item.get("direct") is True
    }
    direct_flags_ok = reported_direct == direct_pages and all(
        isinstance(item, dict)
        and isinstance(item.get("direct"), bool)
        and isinstance(item.get("reasons"), list)
        and bool(item.get("reasons"))
        for item in report["affected"]["pages"]
    )
    impact_score += 3.0 if direct_flags_ok else 0.0
    no_unaffected_reported = not (unaffected_pages & reported_pages)
    impact_score += 3.0 if no_unaffected_reported else 0.0
    add_assert(assertions, "IMPACT-PAGES", reported_pages == expected_pages, f"expected={sorted(expected_pages)} actual={sorted(reported_pages)}")
    add_assert(assertions, "IMPACT-PRECISION", no_unaffected_reported, f"unaffected reported={sorted(unaffected_pages & reported_pages)}")
    add_assert(assertions, "IMPACT-EVIDENCE", direct_flags_ok, f"expected direct={sorted(direct_pages)} actual direct={sorted(reported_direct)}; entries need reasons")

    independent, isolated = independently_run_examples(repo, spec["expected_examples"])
    passed_examples = sum(1 for value in independent.values() if value["passed"])
    total_examples = len(spec["expected_examples"])
    examples_score = 12.0 if total_examples == 0 else 12.0 * passed_examples / total_examples
    report_examples = report_example_map(report)
    unreported_examples = set(spec.get("unreported_examples", []))
    expected_reported_examples = set(spec["expected_examples"]) - unreported_examples
    consistency_count = 0
    for example_id, expected_stdout in spec["expected_examples"].items():
        if example_id in unreported_examples:
            continue
        item = report_examples.get(example_id)
        if expected_stdout is None:
            if item and item.get("status") == "skipped":
                consistency_count += 1
        elif item and item.get("status") == "passed" and item.get("expected_stdout") == expected_stdout and item.get("actual_stdout") == expected_stdout and item.get("exit_code") == 0:
            consistency_count += 1
    validation = report["validation"]
    reported_passed = sum(item.get("status") == "passed" for item in report_examples.values())
    reported_failed = sum(item.get("status") == "failed" for item in report_examples.values())
    validation_consistent = (
        validation["examples_total"] == len(report_examples)
        and validation["examples_passed"] == reported_passed
        and validation["examples_failed"] == reported_failed
    )
    report_exact = set(report_examples) == expected_reported_examples and validation_consistent
    consistency_denominator = len(expected_reported_examples)
    report_fraction = 1.0 if consistency_denominator == 0 else consistency_count / consistency_denominator
    examples_score += 5.0 * report_fraction if report_exact else 3.0 * report_fraction
    examples_score += 3.0 if isolated else 0.0
    add_assert(assertions, "EXAMPLE-EXECUTION", passed_examples == total_examples, json.dumps(independent, sort_keys=True))
    add_assert(assertions, "EXAMPLE-REPORT", report_exact and consistency_count == consistency_denominator, f"expected IDs={sorted(expected_reported_examples)} actual IDs={sorted(report_examples)} consistent={consistency_count}/{consistency_denominator}")
    add_assert(assertions, "EXAMPLE-ISOLATION", isolated, "target repository unchanged by evaluator example replay")

    required_checks = 0
    required_passes = 0
    for path, snippets in spec["required_text"].items():
        text = (repo / path).read_text(encoding="utf-8") if (repo / path).is_file() else ""
        for snippet in snippets:
            required_checks += 1
            required_passes += int(snippet.casefold() in text.casefold())
    for path, snippets in spec["forbidden_text"].items():
        text = (repo / path).read_text(encoding="utf-8") if (repo / path).is_file() else ""
        for snippet in snippets:
            required_checks += 1
            required_passes += int(snippet.casefold() not in text.casefold())
    claims = report["affected"]["stale_claims"]
    actual_kinds = [str(item.get("kind", "")) for item in claims if isinstance(item, dict)]
    expected_kinds = spec["expected_stale_kinds"]
    kinds_passed = sum(any(stale_kind_matches(actual, expected) for actual in actual_kinds) for expected in expected_kinds)
    content_points = 12.0 if required_checks == 0 else 12.0 * required_passes / required_checks
    ledger_shape = all(
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(item.get("id"))
        and isinstance(item.get("page"), str)
        and isinstance(item.get("evidence"), list)
        and bool(item.get("evidence"))
        and isinstance(item.get("resolution"), str)
        and bool(item.get("resolution"))
        for item in claims
    )
    kind_points = 4.0 if not expected_kinds else 4.0 * kinds_passed / len(expected_kinds)
    kind_points += 2.0 if ledger_shape else 0.0
    stale_score = content_points + kind_points
    add_assert(assertions, "STALE-CONTENT", required_passes == required_checks, f"semantic checks={required_passes}/{required_checks}")
    add_assert(assertions, "STALE-LEDGER", kinds_passed == len(expected_kinds) and ledger_shape, f"expected={expected_kinds} actual={actual_kinds} entries_complete={ledger_shape}")

    actual_diff = canonical_doc_diff(repo)
    emitted_diff = diff_path.read_text(encoding="utf-8")
    diff_matches = emitted_diff == actual_diff
    expected_status = "no_changes" if expected_no_changes else "updated"
    paths_exact = changed_pages == expected_pages == report_changed and report.get("status") == expected_status
    unaffected_stable = all(before_docs.get(path) == file_hashes(repo).get(path) for path in unaffected_pages)
    owned_preserved = True
    for path in expected_pages:
        before_path = before_snapshot / path
        after_path = repo / path
        if before_path.is_file() and after_path.is_file():
            owned_preserved = owned_preserved and strip_owned_content(before_path.read_text(encoding="utf-8")) == strip_owned_content(after_path.read_text(encoding="utf-8"))
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in (repo / "openwiki").rglob("*.md"))
    tokens_preserved = all(token in all_text for token in spec["preserved_tokens"])
    anchors_preserved = all(f'id="{anchor}"' in all_text or f"id='{anchor}'" in all_text for anchor in spec["preserved_anchors"])
    minimal_score = (6.0 if paths_exact else 0.0) + (4.0 if unaffected_stable else 0.0) + (4.0 if owned_preserved and tokens_preserved and anchors_preserved else 0.0) + (2.0 if diff_matches else 0.0)
    add_assert(assertions, "MINIMAL-PATHS", paths_exact, f"expected={sorted(expected_pages)} changed={sorted(changed_pages)} report={sorted(report_changed)}")
    add_assert(assertions, "PRESERVE-UNAFFECTED", unaffected_stable, f"controls={sorted(unaffected_pages)}")
    add_assert(assertions, "PRESERVE-HANDWRITTEN", owned_preserved and tokens_preserved and anchors_preserved, f"owned_preserved={owned_preserved} tokens={tokens_preserved} anchors={anchors_preserved}")
    add_assert(assertions, "DIFF-EXACT", diff_matches, "emitted diff equals git documentation diff")

    first_docs = file_hashes(repo)
    first_diff = emitted_diff
    replay = invoke_product(prepared.source_dir, repo) if spec.get("replay") else None
    replay_report, replay_report_errors = parse_impact_report(repo / "impact-report.json") if replay and replay.exit_code == 0 else (None, ["replay failed"])
    replay_report_ok = bool(
        replay_report
        and not replay_report_errors
        and replay_report.get("status") == "no_changes"
        and replay_report.get("changed_paths") == []
        and all(
            replay_report.get("affected", {}).get(key) == []
            for key in ("pages", "examples", "diagrams", "links", "stale_claims")
        )
    )
    replay_ok = bool(
        replay
        and replay.exit_code == 0
        and file_hashes(repo) == first_docs
        and diff_path.read_text(encoding="utf-8") == ""
        and replay_report_ok
    )
    minimal_score += 2.0 if replay_ok else 0.0
    add_assert(assertions, "REPLAY-IDEMPOTENT", replay_ok, f"second run docs stable, diff empty, and no_changes report empty={replay_report_ok}")

    frontmatter_ok = all(validate_frontmatter(repo / path) for path in changed_pages) and report["validation"]["schema_valid"] is True
    link_issues = validate_internal_links(repo)
    mermaid_issues = validate_mermaid(repo)
    expected_links_ok = all(suffix in all_text for suffix in spec["expected_link_suffixes"])
    link_reports = report["affected"]["links"]
    link_reports_ok = len(link_reports) >= int(spec.get("minimum_link_reports", 0)) and all(
        isinstance(item, dict)
        and isinstance(item.get("page"), str)
        and isinstance(item.get("target"), str)
        and item.get("status") in {"valid", "repaired", "broken"}
        for item in link_reports
    )
    expected_diagrams_ok = all(any(diagram_id in str(item.get("id", "")) or diagram_id in str(item.get("page", "")) for item in report["affected"]["diagrams"] if isinstance(item, dict)) for diagram_id in spec["expected_diagrams"])
    schema_link_score = (3.0 if frontmatter_ok else 0.0) + (4.0 if not link_issues and expected_links_ok and link_reports_ok and report["validation"]["links_broken"] == 0 else 0.0) + (3.0 if not mermaid_issues and expected_diagrams_ok else 0.0) + (2.0 if diff_matches and all(line.startswith(("diff --git a/openwiki/", "index ", "--- a/openwiki/", "+++ b/openwiki/", "@@", "+", "-", " ", "\\")) or not line for line in first_diff.splitlines()) else 0.0)
    add_assert(assertions, "SCHEMA-OKF", frontmatter_ok, "affected concept pages retain type front matter")
    add_assert(assertions, "LINK-VALID", not link_issues and expected_links_ok and link_reports_ok and report["validation"]["links_broken"] == 0, f"issues={json.dumps(link_issues)} report_entries={len(link_reports)}")
    add_assert(assertions, "MERMAID-VALID", not mermaid_issues and expected_diagrams_ok, json.dumps(mermaid_issues))

    security = report.get("security", {})
    security_shape = isinstance(security, dict) and isinstance(security.get("ignored_instruction_count"), int) and isinstance(security.get("unsafe_execution_count"), int)
    integrity_ok = protected_repo_hashes(repo) == protected_before and external_case_hashes(case_work_dir) == external_before
    if spec.get("security_case"):
        canary = case_work_dir / "parent-secret.txt"
        integrity_ok = integrity_ok and canary.read_text(encoding="utf-8") == "PARENT-CANARY-MUST-NOT-BE-READ-OR-CHANGED\n"
        integrity_ok = integrity_ok and "PARENT-CANARY" not in json.dumps(report) and security.get("unsafe_execution_count") == 0
    no_changes_ok = True
    if expected_no_changes:
        no_changes_ok = report.get("status") == "no_changes" and not changed_pages and not reported_pages and first_diff == ""
    integration_score = (4.0 if invocation.exit_code == 0 else 0.0) + (2.0 if security_shape else 0.0) + (2.0 if integrity_ok else 0.0) + (2.0 if no_changes_ok else 0.0)
    add_assert(assertions, "SECURITY-BOUNDARY", integrity_ok and security_shape, f"write_scope_and_canary_ok={integrity_ok} security={security}")
    add_assert(assertions, "NEGATIVE-CONTROL", no_changes_ok, f"status={report.get('status')} changed={sorted(changed_pages)}")

    dimensions = {
        "impact_recall_precision": impact_score,
        "executable_examples": examples_score,
        "stale_claims_and_repairs": stale_score,
        "minimal_diff_preservation_idempotency": minimal_score,
        "schema_links_diagrams": schema_link_score,
        "integration_security_compatibility": integration_score,
    }
    score = sum(dimensions.values())
    safety_ceiling = False
    if not integrity_ok:
        score = min(score, 30.0)
        safety_ceiling = True
    return CaseResult(
        case_id,
        True,
        score,
        dimensions,
        assertions,
        invocation,
        replay,
        str(repo) if keep_work else None,
        safety_ceiling,
    )


@dataclass
class ScenarioEvidence:
    primary: CommandResult
    primary_report: dict[str, Any] | None
    primary_report_errors: list[str]
    primary_diff: str | None
    invocations: list[CommandResult]
    recovery_checks: list[tuple[str, bool, str]]
    coordination_checks: list[tuple[str, bool, str]]


def read_response(repo: Path, report_name: str, diff_name: str) -> tuple[dict[str, Any] | None, list[str], str | None]:
    report, errors = parse_impact_report(repo / report_name)
    try:
        diff = (repo / diff_name).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        diff = None
    return report, errors, diff


def report_transaction_matches(report: dict[str, Any] | None, transaction: dict[str, Any], outcomes: set[str]) -> bool:
    if not isinstance(report, dict):
        return False
    value = report.get("transaction")
    if not isinstance(value, dict):
        return False
    return (
        value.get("tenant_id") == transaction.get("tenant_id")
        and value.get("request_id") == transaction.get("request_id")
        and value.get("generation") == transaction.get("generation")
        and value.get("payload_digest") == transaction.get("payload_digest")
        and value.get("outcome") in outcomes
        and isinstance(value.get("receipt_path"), str)
    )


def receipt_is_valid(repo: Path, transaction: dict[str, Any], expected_pages: set[str]) -> tuple[bool, str]:
    paths = transaction_paths(repo, transaction)
    try:
        receipt = load_json(paths["receipt"])
    except HarnessError as exc:
        return False, str(exc)
    identity_ok = all(
        receipt.get(key) == transaction.get(key)
        for key in ("tenant_id", "request_id", "generation", "payload_digest")
    )
    hashes = receipt.get("documentation_hashes")
    hashes_ok = isinstance(hashes, dict) and set(hashes) == expected_pages
    if hashes_ok:
        for relative, digest in hashes.items():
            path = repo / str(relative)
            if not path.is_file() or digest != sha256_file(path):
                hashes_ok = False
                break
    result = receipt.get("result")
    result_ok = (
        isinstance(result, dict)
        and result.get("status") in {"updated", "no_changes"}
        and isinstance(result.get("validation"), dict)
        and isinstance(result.get("security"), dict)
    )
    valid = (
        receipt.get("schema_version") == "1.0"
        and receipt.get("status") == "committed"
        and identity_ok
        and receipt.get("changed_paths") == sorted(expected_pages)
        and hashes_ok
        and result_ok
    )
    return valid, f"identity={identity_ok} hashes={hashes_ok} result={result_ok} path={paths['receipt']}"


def tenant_state_valid(repo: Path, transaction: dict[str, Any]) -> bool:
    try:
        state = load_json(transaction_paths(repo, transaction)["state"])
    except HarnessError:
        return False
    return (
        state.get("schema_version") == "1.0"
        and state.get("tenant_id") == transaction.get("tenant_id")
        and state.get("latest_generation") == transaction.get("generation")
        and state.get("latest_request_id") == transaction.get("request_id")
    )


def cross_surface_checks(
    repo: Path,
    spec: dict[str, Any],
    report: dict[str, Any],
    evidence: ScenarioEvidence,
    publication_ok: bool,
    search_ok: bool,
) -> list[tuple[str, bool, str]]:
    transaction = spec["transaction"]
    publication = spec.get("publication")
    search = spec.get("search")
    receipt_ok, receipt_detail = receipt_is_valid(
        repo, transaction, set(spec["expected_pages"])
    )
    report_ok = report_transaction_matches(report, transaction, {"committed", "recovered"})
    checks: list[tuple[str, bool, str]] = [
        ("CROSS-DURABLE-COMMIT", report_ok and receipt_ok, receipt_detail),
    ]
    try:
        receipt = load_json(transaction_paths(repo, transaction)["receipt"])
    except HarnessError:
        receipt = {}
    digest = str(transaction["payload_digest"])
    publication_receipt = False
    search_receipt = False
    active_pair = False
    if isinstance(publication, dict):
        pp = publication_paths(repo, publication)
        receipt_value = receipt.get("publication")
        publication_receipt = (
            isinstance(receipt_value, dict)
            and _identity_matches(receipt_value, "site_id", publication, digest)
            and pp["manifest"].is_file()
            and receipt_value.get("manifest_sha256") == sha256_file(pp["manifest"])
        )
    if isinstance(search, dict):
        sp = search_paths(repo, search)
        receipt_value = receipt.get("search_index")
        search_receipt = (
            isinstance(receipt_value, dict)
            and _identity_matches(receipt_value, "index_id", search, digest)
            and sp["manifest"].is_file()
            and receipt_value.get("manifest_sha256") == sha256_file(sp["manifest"])
        )
    if isinstance(publication, dict) and isinstance(search, dict):
        try:
            pa = load_json(publication_paths(repo, publication)["active"])
            sa = load_json(search_paths(repo, search)["active"])
        except HarnessError:
            pass
        else:
            active_pair = (
                pa.get("generation") == sa.get("generation") == transaction["generation"]
                and pa.get("payload_digest") == sa.get("payload_digest") == digest
            )
    checks.extend(
        [
            ("CROSS-PUBLICATION-RECEIPT", publication_ok and publication_receipt, f"receipt_publication={receipt.get('publication')}"),
            ("CROSS-SEARCH-RECEIPT", search_ok and search_receipt, f"receipt_search={receipt.get('search_index')}"),
            ("CROSS-ACTIVE-GENERATION", publication_ok and search_ok and active_pair, f"generation={transaction['generation']} digest={digest}"),
            (
                "CROSS-SCENARIO-CONVERGENCE",
                publication_ok
                and search_ok
                and bool(evidence.recovery_checks)
                and bool(evidence.coordination_checks)
                and all(item[1] for item in evidence.recovery_checks + evidence.coordination_checks),
                "all case-specific restart/conflict/concurrency observations passed",
            ),
        ]
    )
    return checks


def transaction_residue(repo: Path, transaction: dict[str, Any]) -> list[str]:
    paths = transaction_paths(repo, transaction)
    residue: list[str] = []
    for key in ("claim", "lock"):
        if paths[key].exists():
            residue.append(paths[key].relative_to(repo).as_posix())
    if paths["staging"].exists() and any(paths["staging"].rglob("*")):
        residue.append(paths["staging"].relative_to(repo).as_posix())
    tenant = paths["tenant"]
    if tenant.exists():
        residue.extend(
            path.relative_to(repo).as_posix()
            for path in tenant.rglob("*")
            if path.is_file() and (path.name.endswith(".tmp") or path.name.startswith(".tmp-"))
        )
    return sorted(set(residue))


def run_transaction_scenario(
    prepared: PreparedCandidate,
    repo: Path,
    spec: dict[str, Any],
) -> ScenarioEvidence:
    scenario = str(spec.get("scenario", "commit_duplicate"))
    transaction = dict(spec["transaction"])
    invocations: list[CommandResult] = []
    recovery: list[tuple[str, bool, str]] = []
    coordination: list[tuple[str, bool, str]] = []

    if scenario == "concurrent_duplicate":
        def configure(manifest: dict[str, Any], suffix: str, owner: str) -> None:
            manifest["report_path"] = f"impact-report-{suffix}.json"
            manifest["diff_path"] = f"documentation-{suffix}.diff"
            manifest["transaction"]["owner_id"] = owner

        update_manifest(repo, "impact-manifest-a.json", lambda value: configure(value, "a", "worker-a"))
        update_manifest(repo, "impact-manifest-b.json", lambda value: configure(value, "b", "worker-b"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(invoke_product, prepared.source_dir, repo, "impact-manifest-a.json"),
                pool.submit(invoke_product, prepared.source_dir, repo, "impact-manifest-b.json"),
            ]
            invocations = [future.result() for future in futures]
        responses = [
            read_response(repo, "impact-report-a.json", "documentation-a.diff"),
            read_response(repo, "impact-report-b.json", "documentation-b.diff"),
        ]
        committed_index = next(
            (index for index, item in enumerate(responses) if report_transaction_matches(item[0], transaction, {"committed", "recovered"})),
            0,
        )
        primary_report, primary_errors, primary_diff = responses[committed_index]
        outcomes = [
            item[0].get("transaction", {}).get("outcome") if isinstance(item[0], dict) else None
            for item in responses
        ]
        coordination.extend(
            [
                ("CONCURRENT-TERMINATES", all(item.exit_code == 0 and not item.timed_out for item in invocations), f"exits={[item.exit_code for item in invocations]}"),
                ("CONCURRENT-OUTCOMES", outcomes.count("committed") == 1 and outcomes.count("duplicate") == 1, f"outcomes={outcomes}"),
                ("CONCURRENT-ONE-RECEIPT", transaction_paths(repo, transaction)["receipt"].is_file(), "one stable request receipt path"),
                ("CONCURRENT-STATE", tenant_state_valid(repo, transaction), "tenant generation advanced once"),
            ]
        )
        recovery.extend(
            [
                ("PRIMARY-SUCCESS", any(item.exit_code == 0 for item in invocations), "at least one delivery committed"),
                ("PRIMARY-REPORT", primary_report is not None and not primary_errors, f"errors={primary_errors}"),
                ("PRIMARY-DIFF", primary_diff is not None, "committing response emitted a diff"),
                ("NO-ABANDONED-STATE", not transaction_residue(repo, transaction), f"residue={transaction_residue(repo, transaction)}"),
                ("RESPONSES-INDEPENDENT", all(item[0] is not None and item[2] is not None for item in responses), "both selected response paths exist"),
            ]
        )
        return ScenarioEvidence(invocations[committed_index], primary_report, primary_errors, primary_diff, invocations, recovery, coordination)

    primary = invoke_product(prepared.source_dir, repo)
    invocations.append(primary)
    primary_report, primary_errors, primary_diff = read_response(repo, "impact-report.json", "documentation.diff")
    docs_after_primary = file_hashes(repo)
    paths = transaction_paths(repo, transaction)
    receipt_before = paths["receipt"].read_bytes() if paths["receipt"].is_file() else None
    state_before = state_tree_hash(paths["tenant"])
    publication_before = state_tree_hash(publication_paths(repo, spec["publication"])["root"]) if isinstance(spec.get("publication"), dict) else "unselected"
    search_before = state_tree_hash(search_paths(repo, spec["search"])["root"]) if isinstance(spec.get("search"), dict) else "unselected"

    recovery.extend(
        [
            ("PRIMARY-SUCCESS", primary.exit_code == 0, f"exit={primary.exit_code}"),
            ("PRIMARY-REPORT", primary_report is not None and not primary_errors, f"errors={primary_errors}"),
        ]
    )

    if scenario in {"commit_duplicate", "lost_response_restart", "stale_owner_restart"}:
        if scenario == "stale_owner_restart":
            recovery.append(
                ("INITIAL-RECOVERY", report_transaction_matches(primary_report, transaction, {"recovered"}), "expired foreign claim was recovered")
            )
        for name in ("impact-report.json", "documentation.diff"):
            path = repo / name
            if path.exists():
                path.unlink()
        update_manifest(repo, "impact-manifest-retry.json", lambda value: value["transaction"].update({"owner_id": "restart-worker"}))
        retry = invoke_product(prepared.source_dir, repo, "impact-manifest-retry.json")
        invocations.append(retry)
        duplicate_report, duplicate_errors, duplicate_diff = read_response(repo, "impact-report.json", "documentation.diff")
        recovery.extend(
            [
                ("LOST-RESPONSE-REBUILT", duplicate_report is not None and not duplicate_errors and duplicate_diff == "", f"errors={duplicate_errors} diff_empty={duplicate_diff == ''}"),
                ("RETRY-DOCS-STABLE", file_hashes(repo) == docs_after_primary, "retry did not rewrite documentation"),
                ("RETRY-RECEIPT-STABLE", receipt_before is not None and paths["receipt"].read_bytes() == receipt_before, "immutable receipt bytes preserved"),
                ("RETRY-PUBLICATION-STABLE", not isinstance(spec.get("publication"), dict) or state_tree_hash(publication_paths(repo, spec["publication"])["root"]) == publication_before, "duplicate kept publication byte-stable"),
                ("RETRY-SEARCH-STABLE", not isinstance(spec.get("search"), dict) or state_tree_hash(search_paths(repo, spec["search"])["root"]) == search_before, "duplicate kept search byte-stable"),
            ]
        )
        coordination.extend(
            [
                ("DUPLICATE-SUCCESS", retry.exit_code == 0 and not retry.timed_out, f"exit={retry.exit_code}"),
                ("DUPLICATE-OUTCOME", report_transaction_matches(duplicate_report, transaction, {"duplicate"}), "retry is receipt-backed duplicate"),
                ("GENERATION-ONCE", tenant_state_valid(repo, transaction), "tenant generation remains committed identity"),
                ("NO-ABANDONED-STATE", not transaction_residue(repo, transaction), f"residue={transaction_residue(repo, transaction)}"),
            ]
        )
        if spec.get("post_commit_corrupt_search") and isinstance(spec.get("search"), dict):
            search_config = spec["search"]
            active_path = search_paths(repo, search_config)["active"]
            original_active = active_path.read_bytes() if active_path.is_file() else None
            if original_active is not None:
                active_path.write_text('{"schema_version":"1.0","corrupt":true}\n', encoding="utf-8")
            corrupt_query = invoke_search(
                prepared.source_dir,
                repo,
                search_config,
                str(spec.get("corrupt_probe_query", "probe")),
                3,
            )
            invocations.append(corrupt_query)
            update_manifest(repo, "impact-manifest-corrupt-retry.json", lambda value: value["transaction"].update({"owner_id": "corrupt-retry-worker"}))
            corrupt_retry = invoke_product(prepared.source_dir, repo, "impact-manifest-corrupt-retry.json")
            invocations.append(corrupt_retry)
            coordination.extend(
                [
                    ("CORRUPT-SEARCH-QUERY-FAILS", corrupt_query.exit_code != 0 and parse_single_json_stdout(corrupt_query) is None, f"exit={corrupt_query.exit_code}"),
                    ("CORRUPT-EVIDENCE-RETRY-FAILS", corrupt_retry.exit_code != 0 and not corrupt_retry.timed_out, f"exit={corrupt_retry.exit_code}"),
                    ("CORRUPT-RETRY-DOCS-STABLE", file_hashes(repo) == docs_after_primary, "failed retry changed no docs"),
                    ("CORRUPT-RETRY-RECEIPT-STABLE", receipt_before is not None and paths["receipt"].read_bytes() == receipt_before, "failed retry did not rewrite receipt"),
                ]
            )
            if original_active is not None:
                active_path.write_bytes(original_active)
    elif scenario == "recover_conflict":
        recovery.extend(
            [
                ("RECOVERED-OUTCOME", report_transaction_matches(primary_report, transaction, {"recovered"}), "expired claim recovered"),
                ("INCOMPLETE-STAGE-CLEANED", not paths["staging"].exists() or not any(paths["staging"].rglob("*")), f"staging={paths['staging']}"),
                ("RECOVERY-RECEIPT", receipt_before is not None, "recovery published receipt"),
            ]
        )
        conflict_digest = "3" * 64
        def configure_conflict(value: dict[str, Any]) -> None:
            value["report_path"] = "impact-report-conflict.json"
            value["diff_path"] = "documentation-conflict.diff"
            value["transaction"]["payload_digest"] = conflict_digest
            value["transaction"]["owner_id"] = "conflict-worker"
        update_manifest(repo, "impact-manifest-conflict.json", configure_conflict)
        conflict = invoke_product(prepared.source_dir, repo, "impact-manifest-conflict.json")
        invocations.append(conflict)
        conflict_report, _, _ = read_response(repo, "impact-report-conflict.json", "documentation-conflict.diff")
        coordination.extend(
            [
                ("CONFLICT-REJECTED", conflict.exit_code != 0 and not conflict.timed_out, f"exit={conflict.exit_code}"),
                ("CONFLICT-REPORTED", isinstance(conflict_report, dict) and conflict_report.get("transaction", {}).get("outcome") == "conflict", "failed report records conflict"),
                ("CONFLICT-DOCS-STABLE", file_hashes(repo) == docs_after_primary, "conflict changed no docs"),
                ("CONFLICT-STATE-STABLE", state_tree_hash(paths["tenant"]) == state_before, "conflict changed no receipt or generation"),
                ("CONFLICT-SURFACES-STABLE", (not isinstance(spec.get("publication"), dict) or state_tree_hash(publication_paths(repo, spec["publication"])["root"]) == publication_before) and (not isinstance(spec.get("search"), dict) or state_tree_hash(search_paths(repo, spec["search"])["root"]) == search_before), "conflict changed no active surface"),
            ]
        )
    elif scenario == "generation_tenant_isolation":
        recovery.extend(
            [
                ("PRIMARY-COMMITTED", report_transaction_matches(primary_report, transaction, {"committed"}), "first tenant committed"),
                ("PRIMARY-RECEIPT", receipt_before is not None, "first tenant receipt exists"),
                ("PRIMARY-STATE", tenant_state_valid(repo, transaction), "first tenant state valid"),
            ]
        )
        def configure_stale(value: dict[str, Any]) -> None:
            value["report_path"] = "impact-report-stale.json"
            value["diff_path"] = "documentation-stale.diff"
            value["transaction"].update({"request_id": "older-004", "generation": 4, "payload_digest": "4" * 64, "owner_id": "stale-worker"})
        update_manifest(repo, "impact-manifest-stale.json", configure_stale)
        stale = invoke_product(prepared.source_dir, repo, "impact-manifest-stale.json")
        invocations.append(stale)
        red_after_stale = state_tree_hash(paths["tenant"])
        def configure_blue(value: dict[str, Any]) -> None:
            value["report_path"] = "impact-report-blue.json"
            value["diff_path"] = "documentation-blue.diff"
            value["transaction"].update({"tenant_id": "reader-blue", "request_id": "migration-blue-001", "generation": 1, "payload_digest": "5" * 64, "owner_id": "blue-worker"})
            value.pop("publication", None)
            value.pop("search", None)
        blue_manifest = update_manifest(repo, "impact-manifest-blue.json", configure_blue)
        blue_tx = blue_manifest["transaction"]
        blue = invoke_product(prepared.source_dir, repo, "impact-manifest-blue.json")
        invocations.append(blue)
        coordination.extend(
            [
                ("STALE-REJECTED", stale.exit_code != 0 and not stale.timed_out, f"exit={stale.exit_code}"),
                ("STALE-STATE-STABLE", red_after_stale == state_before and file_hashes(repo) == docs_after_primary, "stale request changed no first-tenant state/docs"),
                ("SECOND-TENANT-COMMITS", blue.exit_code == 0 and tenant_state_valid(repo, blue_tx), f"exit={blue.exit_code}"),
                ("TENANTS-INDEPENDENT", state_tree_hash(paths["tenant"]) == red_after_stale and transaction_paths(repo, blue_tx)["receipt"].is_file(), "second tenant preserved first tenant"),
            ]
        )
    elif scenario == "partial_staging_recovery":
        recovery.extend(
            [
                ("RECOVERED-OUTCOME", report_transaction_matches(primary_report, transaction, {"recovered"}), "prepared request recovered"),
                ("CORRUPT-STAGE-DISCARDED", not paths["staging"].exists() or not any(paths["staging"].rglob("*")), "truncated staged bytes removed"),
                ("RECOVERY-RECEIPT", receipt_before is not None, "receipt published after installed docs"),
            ]
        )
        coordination.extend(
            [
                ("RECOVERY-GENERATION", tenant_state_valid(repo, transaction), "tenant state advanced to recovered request"),
                ("ONE-RECEIPT", paths["receipt"].is_file(), "one request receipt"),
                ("NO-LIVE-OWNER", not paths["claim"].exists() and not paths["lock"].exists(), "no stale owner state"),
                ("NO-TEMP-STATE", not transaction_residue(repo, transaction), f"residue={transaction_residue(repo, transaction)}"),
                ("PARTIAL-PUBLICATION-REPLACED", not isinstance(spec.get("publication"), dict) or not (publication_paths(repo, spec["publication"])["release"] / ".partial-untrusted").exists(), "untrusted partial release removed"),
                ("PARTIAL-SEARCH-REPLACED", not isinstance(spec.get("search"), dict) or not (search_paths(repo, spec["search"])["generation"] / ".partial-untrusted").exists(), "untrusted partial index removed"),
            ]
        )
    elif scenario == "ordinary_and_tenant_isolation":
        recovery.extend(
            [
                ("TENANT-A-COMMITS", primary.exit_code == 0 and receipt_before is not None, "first no-change receipt committed"),
                ("TENANT-A-TRUTHFUL", report_transaction_matches(primary_report, transaction, {"committed"}), "first coordinated report truthful"),
                ("TENANT-A-DOCS-STABLE", file_hashes(repo) == docs_after_primary, "no user documentation impact"),
            ]
        )
        abandoned = paths["tenant"] / "claims" / "abandoned.json"
        write_json(abandoned, {"schema_version": "0", "malformed": True})
        def configure_tenant_b(value: dict[str, Any]) -> None:
            value["report_path"] = "impact-report-b.json"
            value["diff_path"] = "documentation-b.diff"
            value["transaction"].update({"tenant_id": "security-b", "request_id": "noop-b-001", "generation": 1, "payload_digest": "6" * 64, "owner_id": "worker-b"})
            value.pop("publication", None)
            value.pop("search", None)
        b_manifest = update_manifest(repo, "impact-manifest-b.json", configure_tenant_b)
        tenant_b = b_manifest["transaction"]
        second = invoke_product(prepared.source_dir, repo, "impact-manifest-b.json")
        invocations.append(second)
        state_before_ordinary = state_tree_hash(paths["root"])
        def configure_ordinary(value: dict[str, Any]) -> None:
            value["report_path"] = "impact-report-ordinary.json"
            value["diff_path"] = "documentation-ordinary.diff"
            value.pop("transaction", None)
            value.pop("publication", None)
            value.pop("search", None)
        update_manifest(repo, "impact-manifest-ordinary.json", configure_ordinary)
        ordinary = invoke_product(prepared.source_dir, repo, "impact-manifest-ordinary.json")
        invocations.append(ordinary)
        ordinary_report, ordinary_errors, ordinary_diff = read_response(repo, "impact-report-ordinary.json", "documentation-ordinary.diff")
        coordination.extend(
            [
                ("SECOND-TENANT-USABLE", second.exit_code == 0 and tenant_state_valid(repo, tenant_b), f"exit={second.exit_code}"),
                ("CORRUPTION-ISOLATED", abandoned.is_file() and transaction_paths(repo, tenant_b)["receipt"].is_file(), "tenant A abandoned state did not block tenant B"),
                ("ORDINARY-MODE", ordinary.exit_code == 0 and ordinary_report is not None and not ordinary_errors and ordinary_report.get("status") == "no_changes" and ordinary_diff == "", f"exit={ordinary.exit_code} errors={ordinary_errors}"),
                ("ORDINARY-IGNORES-STATE", state_tree_hash(paths["root"]) == state_before_ordinary and "transaction" not in (ordinary_report or {}), "ordinary mode left transaction tree byte-stable"),
            ]
        )
    else:
        raise HarnessError(f"unsupported transaction scenario: {scenario}")

    return ScenarioEvidence(primary, primary_report, primary_errors, primary_diff, invocations, recovery, coordination)


def score_boolean_checks(checks: list[tuple[str, bool, str]], weight: float, assertions: list[Assertion]) -> float:
    if not checks:
        return 0.0
    for assertion_id, passed, detail in checks:
        add_assert(assertions, assertion_id, passed, detail)
    return weight * sum(int(item[1]) for item in checks) / len(checks)


def evaluate_case(
    prepared: PreparedCandidate,
    case_dir: Path,
    case_work_dir: Path,
    *,
    keep_work: bool = False,
    spec_override: dict[str, Any] | None = None,
) -> CaseResult:
    repo, spec, before_snapshot = materialize_case(case_dir, case_work_dir, spec_override)
    case_id = str(spec["case_id"])
    before_docs = file_hashes(repo)
    protected_before = protected_repo_hashes(repo)
    external_before = external_case_hashes(case_work_dir)
    assertions: list[Assertion] = []

    evidence = run_transaction_scenario(prepared, repo, spec)
    invocation = evidence.primary
    report = evidence.primary_report
    if invocation.exit_code != 0:
        add_assert(assertions, "VALID-PRIMARY-CLI", False, f"CLI exit {invocation.exit_code}: {invocation.stderr[-1000:]}")
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, scenario_invocations=evidence.invocations, worktree=str(repo) if keep_work else None)
    add_assert(assertions, "VALID-PRIMARY-CLI", True, "production impact invocation exited 0")
    if report is None or evidence.primary_report_errors:
        add_assert(assertions, "VALID-PRIMARY-REPORT", False, "; ".join(evidence.primary_report_errors) or "report missing")
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, scenario_invocations=evidence.invocations, worktree=str(repo) if keep_work else None)
    add_assert(assertions, "VALID-PRIMARY-REPORT", True, "primary report schema valid")
    if evidence.primary_diff is None:
        add_assert(assertions, "VALID-PRIMARY-DIFF", False, "primary documentation diff missing or unreadable")
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, scenario_invocations=evidence.invocations, worktree=str(repo) if keep_work else None)
    temp_diff = repo / "documentation-primary-evaluator.diff"
    temp_diff.write_text(evidence.primary_diff, encoding="utf-8")
    diff_valid, diff_detail = validate_documentation_diff(repo, temp_diff)
    temp_diff.unlink(missing_ok=True)
    add_assert(assertions, "VALID-PRIMARY-DIFF", diff_valid, diff_detail)
    if not diff_valid:
        return CaseResult(case_id, False, 0.0, {}, assertions, invocation, scenario_invocations=evidence.invocations, worktree=str(repo) if keep_work else None)

    expected_pages = set(spec["expected_pages"])
    unaffected_pages = set(spec["unaffected_pages"])
    reported_pages = report_page_paths(report)
    reported_direct = {
        item.get("path") for item in report["affected"]["pages"]
        if isinstance(item, dict) and item.get("direct") is True
    }
    page_points = 4.0 if reported_pages == expected_pages and reported_direct == set(spec["direct_pages"]) else 2.0 * len(expected_pages & reported_pages) / max(1, len(expected_pages))
    add_assert(assertions, "SEMANTIC-PAGES", reported_pages == expected_pages, f"expected={sorted(expected_pages)} actual={sorted(reported_pages)} direct={sorted(str(item) for item in reported_direct)}")
    content_total = content_passed = 0
    for path, snippets in spec["required_text"].items():
        text_value = (repo / path).read_text(encoding="utf-8") if (repo / path).is_file() else ""
        for snippet in snippets:
            content_total += 1
            content_passed += int(snippet.casefold() in text_value.casefold())
    for path, snippets in spec["forbidden_text"].items():
        text_value = (repo / path).read_text(encoding="utf-8") if (repo / path).is_file() else ""
        for snippet in snippets:
            content_total += 1
            content_passed += int(snippet.casefold() not in text_value.casefold())
    content_points = 5.0 if content_total == 0 else 5.0 * content_passed / content_total
    add_assert(assertions, "SEMANTIC-CONTENT", content_passed == content_total, f"checks={content_passed}/{content_total}")
    actual_kinds = [str(item.get("kind", "")) for item in report["affected"]["stale_claims"] if isinstance(item, dict)]
    kind_passes = sum(any(stale_kind_matches(actual, expected) for actual in actual_kinds) for expected in spec["expected_stale_kinds"])
    kind_points = 3.0 if not spec["expected_stale_kinds"] else 3.0 * kind_passes / len(spec["expected_stale_kinds"])
    add_assert(assertions, "SEMANTIC-LEDGER", kind_passes == len(spec["expected_stale_kinds"]), f"expected={spec['expected_stale_kinds']} actual={actual_kinds}")
    semantic_score = page_points + content_points + kind_points

    independent, isolated = independently_run_examples(repo, spec["expected_examples"])
    example_passes = sum(int(value["passed"]) for value in independent.values())
    example_total = len(independent)
    execution_points = 5.0 if example_total == 0 else 5.0 * example_passes / example_total
    report_examples = report_example_map(report)
    expected_reported = set(spec["expected_examples"]) - set(spec.get("unreported_examples", []))
    report_ok = set(report_examples) == expected_reported and all(
        spec["expected_examples"][example_id] is None
        or (
            report_examples[example_id].get("status") == "passed"
            and report_examples[example_id].get("actual_stdout") == spec["expected_examples"][example_id]
        )
        for example_id in expected_reported
    )
    example_score = execution_points + (2.0 if report_ok else 0.0) + (1.0 if isolated else 0.0)
    add_assert(assertions, "EXAMPLE-EXECUTION", example_passes == example_total, json.dumps(independent, sort_keys=True))
    add_assert(assertions, "EXAMPLE-REPORT", report_ok, f"expected={sorted(expected_reported)} actual={sorted(report_examples)}")
    add_assert(assertions, "EXAMPLE-ISOLATION", isolated, "independent replay left target unchanged")

    changed_pages = changed_doc_paths(repo)
    canonical_diff = canonical_doc_diff(repo)
    paths_ok = changed_pages == expected_pages and set(report.get("changed_paths", [])) == expected_pages
    diff_exact = evidence.primary_diff == canonical_diff
    unaffected_ok = all(before_docs.get(path) == file_hashes(repo).get(path) for path in unaffected_pages)
    owned_ok = True
    for path in expected_pages:
        if (before_snapshot / path).is_file() and (repo / path).is_file():
            owned_ok = owned_ok and strip_owned_content((before_snapshot / path).read_text(encoding="utf-8")) == strip_owned_content((repo / path).read_text(encoding="utf-8"))
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in (repo / "openwiki").rglob("*.md"))
    preservation_ok = owned_ok and unaffected_ok and all(token in all_text for token in spec["preserved_tokens"]) and all(f'id="{anchor}"' in all_text or f"id='{anchor}'" in all_text for anchor in spec["preserved_anchors"])
    structural_ok = all(validate_frontmatter(repo / path) for path in changed_pages) and not validate_internal_links(repo) and not validate_mermaid(repo)
    artifact_score = (3.0 if paths_ok and diff_exact else 0.0) + (3.0 if preservation_ok else 0.0) + (2.0 if structural_ok else 0.0) + (2.0 if report.get("validation", {}).get("schema_valid") is True and report.get("validation", {}).get("links_broken") == 0 else 0.0)
    add_assert(assertions, "ARTIFACT-PATHS-DIFF", paths_ok and diff_exact, f"changed={sorted(changed_pages)} report={report.get('changed_paths')} diff_exact={diff_exact}")
    add_assert(assertions, "ARTIFACT-PRESERVATION", preservation_ok, f"unaffected={unaffected_ok} owned={owned_ok}")
    add_assert(assertions, "ARTIFACT-STRUCTURE", structural_ok, f"links={validate_internal_links(repo)} mermaid={validate_mermaid(repo)}")

    for assertion_id, passed, detail in evidence.recovery_checks + evidence.coordination_checks:
        add_assert(assertions, assertion_id, passed, detail)

    publication_checks = validate_publication_surface(repo, spec, report)
    publication_score = score_boolean_checks(publication_checks, 30.0, assertions)
    publication_ok = len(publication_checks) >= 6 and all(item[1] for item in publication_checks)

    search_checks, search_invocations = validate_search_surface(
        prepared.source_dir, repo, spec, report
    )
    evidence.invocations.extend(search_invocations)
    search_score = score_boolean_checks(search_checks, 30.0, assertions)
    search_ok = len(search_checks) >= 6 and all(item[1] for item in search_checks)

    transaction = spec["transaction"]
    report_tx_ok = report_transaction_matches(report, transaction, {"committed", "recovered"})
    receipt_ok, receipt_detail = receipt_is_valid(repo, transaction, expected_pages)
    add_assert(assertions, "TRANSACTION-REPORT", report_tx_ok, f"transaction={report.get('transaction')}")
    add_assert(assertions, "TRANSACTION-RECEIPT", receipt_ok, receipt_detail)

    cross_checks = cross_surface_checks(
        repo, spec, report, evidence, publication_ok, search_ok
    )
    cross_score = score_boolean_checks(cross_checks, 20.0, assertions)

    security = report.get("security", {})
    integrity_ok = protected_repo_hashes(repo) == protected_before and external_case_hashes(case_work_dir) == external_before
    if spec.get("security_case"):
        canary = case_work_dir / "parent-secret.txt"
        integrity_ok = integrity_ok and canary.read_text(encoding="utf-8") == "PARENT-CANARY-MUST-NOT-BE-READ-OR-CHANGED\n" and "PARENT-CANARY" not in json.dumps(report) and security.get("unsafe_execution_count") == 0
    invocations_bounded = all(not item.timed_out and not item.memory_exceeded for item in evidence.invocations)
    integration_score = (2.0 if isinstance(security.get("ignored_instruction_count"), int) and isinstance(security.get("unsafe_execution_count"), int) else 0.0) + (2.0 if integrity_ok else 0.0) + (1.0 if invocations_bounded else 0.0)
    add_assert(assertions, "INTEGRATION-SECURITY", integrity_ok, f"write_scope_and_canary={integrity_ok}")
    add_assert(assertions, "INTEGRATION-BOUNDED", invocations_bounded, f"invocations={len(evidence.invocations)}")

    preserved_score = (semantic_score / 12.0 * 7.0) + (example_score / 8.0 * 3.0) + (artifact_score / 10.0 * 5.0)
    dimensions = {
        "preserved_executable_docs": preserved_score,
        "static_publication": publication_score,
        "full_text_search": search_score,
        "cross_surface_convergence": cross_score,
        "production_compatibility_security": integration_score,
    }
    score = sum(dimensions.values())
    availability_ceiling = not publication_ok or not search_ok or not report_tx_ok or not receipt_ok
    if availability_ceiling:
        configured = spec.get("ceilings", {})
        surface_limit = int(configured.get("surface_availability", 25)) if isinstance(configured, dict) else 25
        score = min(score, 20.0 if not publication_ok and not search_ok else surface_limit)
    safety_ceiling = not integrity_ok
    if safety_ceiling:
        configured = spec.get("ceilings", {})
        safety_limit = int(configured.get("safety_boundary", 30)) if isinstance(configured, dict) else 30
        score = min(score, safety_limit)
    return CaseResult(
        case_id=case_id,
        valid=True,
        score=score,
        dimension_scores=dimensions,
        assertions=assertions,
        invocation=invocation,
        scenario_invocations=evidence.invocations,
        worktree=str(repo) if keep_work else None,
        safety_ceiling_applied=safety_ceiling,
        availability_ceiling_applied=availability_ceiling,
    )


def public_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one public OpenWiki edit case")
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--case", required=True, choices=["dev_001", "dev_002"])
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--skip-install", action="store_true", help="Only for harness self-tests with a prebuilt candidate")
    return parser
