from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "input" / "repository"
SOURCE_TREE_SHA256 = "cc1490ea633bfed752c25d5d89bfa8fd7aa95afb740029d81c94c9fd5fc60954"
CASES = {
    "dev_001": {"tenant": "lab-alpha", "project": "affine-project", "actors": {"science": "reviewer-science", "code": "reviewer-code", "operator": "operator-local", "auditor": "review-audit", "security": "security-operator"}},
    "dev_002": {"tenant": "lab-beta", "project": "selection-project", "actors": {"science": "reviewer-science", "code": "reviewer-code", "operator": "operator-local", "auditor": "review-audit", "security": "security-operator"}},
}


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int = 600) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, start_new_session=True)
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.wait(.05):
            pending, seen, rss = [process.pid], set(), 0
            while pending:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    status = Path(f"/proc/{pid}/status").read_text()
                    rss += int(next(line for line in status.splitlines() if line.startswith("VmRSS:")).split()[1]) * 1024
                    pending.extend(int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
                except (OSError, StopIteration, ValueError, IndexError):
                    pass
            peak = max(peak, rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    stop.set()
    thread.join(timeout=1)
    return {"argv": argv, "exit_code": process.returncode, "timed_out": timed_out, "duration_seconds": round(time.monotonic() - started, 6), "peak_memory_bytes": peak, "stdout": stdout.decode(errors="replace")[-131072:], "stderr": stderr.decode(errors="replace")[-131072:]}


def prepare(submission: Path) -> tuple[Path, Path, list[str]]:
    if source_digest(SOURCE) != SOURCE_TREE_SHA256:
        raise RuntimeError("pinned input/repository source hash mismatch")
    required = {"solution.patch", "edit_report.json", "run_report.json"}
    if {path.name for path in submission.iterdir()} != required:
        raise RuntimeError("delivery must contain exactly solution.patch, edit_report.json, and run_report.json")
    patch = submission / "solution.patch"
    text = patch.read_text(encoding="utf-8")
    paths = sorted({value for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", text, re.MULTILINE) for value in match.groups()})
    if not text.strip() or any(Path(path).is_absolute() or ".." in Path(path).parts or not (path.startswith(("core/", "workflows/", "tests/", "docs/", "prompts/")) or path in {"setup.py", "pyproject.toml", "requirements.txt"}) for path in paths):
        raise RuntimeError(f"patch is empty or changes a forbidden path: {paths}")
    edit = json.loads((submission / "edit_report.json").read_text())
    report = json.loads((submission / "run_report.json").read_text())
    if edit.get("schema_version") != "1.0" or not isinstance(edit.get("feature_summary"), str) or not edit["feature_summary"].strip() or not isinstance(edit.get("commands"), list) or not isinstance(edit.get("compatibility_notes"), (str, list)) or not isinstance(edit.get("limitations"), (str, list)) or sorted(edit.get("changed_paths", [])) != paths:
        raise RuntimeError("edit_report schema or changed_paths mismatch")
    if set(report) != {"schema_version", "status", "artifact_paths", "errors", "runtime_seconds", "peak_memory_bytes", "api_calls"} or report.get("schema_version") != "1.0" or report.get("artifact_paths") != ["solution.patch", "edit_report.json", "run_report.json"] or not isinstance(report.get("status"), str) or not report["status"] or not isinstance(report.get("errors"), list) or any(not isinstance(value, str) for value in report["errors"]):
        raise RuntimeError("run_report delivery schema mismatch")
    if isinstance(report.get("runtime_seconds"), bool) or not isinstance(report.get("runtime_seconds"), (int, float)) or report["runtime_seconds"] < 0 or isinstance(report.get("peak_memory_bytes"), bool) or not isinstance(report.get("peak_memory_bytes"), int) or report["peak_memory_bytes"] < 0:
        raise RuntimeError("run_report resource fields are invalid")
    if not isinstance(report.get("api_calls"), dict) or set(report["api_calls"]) != {"gateway", "serper", "web_retrieval"} or any(isinstance(report["api_calls"][key], bool) or not isinstance(report["api_calls"].get(key), int) or report["api_calls"][key] < 0 for key in ("gateway", "serper", "web_retrieval")):
        raise RuntimeError("run_report api_calls are invalid")
    workspace = Path(tempfile.mkdtemp(prefix="deepcode-public-"))
    repository = workspace / "repository"
    shutil.copytree(SOURCE, repository)
    for command in (["git", "init", "-q"], ["git", "config", "user.email", "benchmark@invalid.local"], ["git", "config", "user.name", "Benchmark"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "baseline"], ["git", "apply", "--check", str(patch.resolve())], ["git", "apply", str(patch.resolve())]):
        result = run(command, repository, {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, 120)
        if result["exit_code"] != 0:
            raise RuntimeError(f"patch application failed: {result['stderr']}")
    if run(["git", "apply", "--check", str(patch.resolve())], repository, os.environ.copy(), 120)["exit_code"] == 0:
        raise RuntimeError("solution.patch applies more than once")
    status = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], repository, os.environ.copy(), 120)
    actual = sorted(item[3:] for item in status["stdout"].split("\0") if item)
    if actual != paths:
        raise RuntimeError(f"applied paths differ from patch paths: {actual} != {paths}")
    return workspace, repository, paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    assertions: list[dict] = []
    processes: list[dict] = []
    workspace = None

    def check(name: str, passed: bool, evidence: object) -> None:
        assertions.append({"id": name, "passed": bool(passed), "evidence": str(evidence)[:1800]})

    try:
        workspace, repository, changed = prepare(args.submission.resolve())
        env = {**os.environ, "PYTHONPATH": str(repository), "PYTHONDONTWRITEBYTECODE": "1", "DEEPCODE_OFFLINE": "1", "NO_PROXY": "*", "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}
        for key in ("GATEWAY_API_KEY", "SERPER_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        gates = [run([sys.executable, "-m", "compileall", "-q", "-x", r"codebase_index_workflow\.py$", "core", "workflows"], repository, env), run([sys.executable, "-m", "pytest", "-q", "tests/test_verification.py", "tests/test_workflow_compatibility.py", "tests/test_unified_impl_workflow.py"], repository, env)]
        processes.extend(gates)
        check("PUBLIC-PATCH-GATES", all(item["exit_code"] == 0 and not item["timed_out"] for item in gates), changed)
        project = workspace / "project"
        shutil.copytree(ROOT / "dev_cases" / args.case / "assets" / "project", project)
        direct = project / ".deepcode" / "candidate_capsules" / "base"
        direct.parent.mkdir(parents=True, exist_ok=True)
        request = project / ".deepcode" / "traceability_request.json"
        capsule = run([sys.executable, "-m", "workflows.traceability", "--request", str(request), "--output", str(direct)], repository, env)
        processes.append(capsule)
        values = json.loads((direct / "reproduction_manifest.json").read_text())
        check("PUBLIC-CAPSULE", capsule["exit_code"] == 0 and values.get("schema_version") == "1.0", values)
        successor = project / ".deepcode" / "candidate_capsules" / "successor"
        shutil.copytree(direct, successor)
        graph_path = successor / "traceability_graph.json"
        graph = json.loads(graph_path.read_text())
        graph.setdefault("nodes", []).append({"id": "PUBLIC-REVISION-EVIDENCE", "type": "test", "provenance": "test", "locator": "public input"})
        graph_path.write_text(json.dumps(graph, sort_keys=True) + "\n")
        checksums_path = successor / "checksums.json"
        checksums = json.loads(checksums_path.read_text())
        checksums["files"]["traceability_graph.json"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        checksums_path.write_text(json.dumps(checksums, sort_keys=True) + "\n")
        spec = CASES[args.case]
        operation_dir = project / ".deepcode" / "operations"
        operation_dir.mkdir(parents=True, exist_ok=True)
        revision_store = project / ".deepcode" / "traceability_revisions"
        execution_store = project / ".deepcode" / "traceability_execution"

        def op(action: str, operation_id: str, actor: str, **fields: object) -> tuple[dict, dict | None]:
            document = {"schema_version": "1.0", "operation_id": operation_id, "action": action, "tenant_id": spec["tenant"], "project_id": spec["project"], "actor_id": actor, **fields}
            path = operation_dir / f"{operation_id}.json"
            path.write_text(json.dumps(document, sort_keys=True) + "\n")
            module = "workflows.traceability_revisions" if action in {"register", "compare", "inspect", "review", "promote", "audit", "quarantine", "restore", "reconcile"} else "workflows.traceability_execution"
            argv = [sys.executable, "-m", module, "--store", str(revision_store if module.endswith("revisions") else execution_store)]
            if module.endswith("execution"):
                argv += ["--revision-store", str(revision_store)]
            argv += ["--operation", str(path)]
            result = run(argv, repository, env)
            processes.append(result)
            try:
                response = json.loads(result["stdout"].strip())
            except json.JSONDecodeError:
                response = None
            return result, response if isinstance(response, dict) else None

        def ok(call: tuple[dict, dict | None], action: str) -> bool:
            return call[0]["exit_code"] == 0 and isinstance(call[1], dict) and call[1].get("accepted") is True and call[1].get("action") == action

        base = op("register", f"{args.case}-register-base", spec["actors"]["science"], revision_key="base", capsule_path="../candidate_capsules/base", parent_revision_id=None, expected_head_revision_id=None)
        base_id = (base[1] or {}).get("record", base[1] or {}).get("revision_id")
        target = op("register", f"{args.case}-register-successor", spec["actors"]["science"], revision_key="successor", capsule_path="../candidate_capsules/successor", parent_revision_id=base_id, expected_head_revision_id=None)
        target_record = (target[1] or {}).get("record", target[1] or {})
        target_id, target_digest = target_record.get("revision_id"), target_record.get("revision_digest")
        compare = op("compare", f"{args.case}-compare", spec["actors"]["science"], base_revision_id=base_id, target_revision_id=target_id)
        check("PUBLIC-REVISION-DIFF", ok(base, "register") and ok(target, "register") and ok(compare, "compare") and "added" in json.dumps(compare[1]).lower(), compare[1])
        science = op("review", f"{args.case}-review-science", spec["actors"]["science"], revision_id=target_id, revision_digest=target_digest, role="scientist", decision="approve", expected_review_generation=target_record.get("review_generation", 0), note="public science review")
        generation = (science[1] or {}).get("record", science[1] or {}).get("review_generation")
        code = op("review", f"{args.case}-review-code", spec["actors"]["code"], revision_id=target_id, revision_digest=target_digest, role="maintainer", decision="approve", expected_review_generation=generation, note="public code review")
        start = op("start", f"{args.case}-start", spec["actors"]["operator"], plan_key="public-plan", revision_id=target_id, revision_digest=target_digest, expected_review_generation=(code[1] or {}).get("record", code[1] or {}).get("review_generation"))
        plan = (start[1] or {}).get("record", start[1] or {})
        check("PUBLIC-REVIEW-AND-START", ok(science, "review") and ok(code, "review") and ok(start, "start") and plan.get("revision_digest") == target_digest, {"science": science[1], "code": code[1], "start": start[1]})
        if args.case == "dev_002":
            pause = op("pause", f"{args.case}-pause", spec["actors"]["operator"], plan_id=plan.get("plan_id"), generation=plan.get("generation"), claim_token=plan.get("claim_token"), worker_id="public-worker", reason={"code": "QUOTA_EXHAUSTED"})
            resume = op("resume", f"{args.case}-resume", spec["actors"]["operator"], plan_id=plan.get("plan_id"), expected_generation=plan.get("generation"), reason="public retry")
            check("PUBLIC-PAUSE-RESUME", ok(pause, "pause") and ok(resume, "resume"), {"pause": pause[1], "resume": resume[1]})
            plan = (resume[1] or {}).get("record", resume[1] or {})
        completed = False
        for index in range(10):
            advanced = op("advance", f"{args.case}-advance-{index}", spec["actors"]["operator"], plan_id=plan.get("plan_id"), generation=plan.get("generation"), claim_token=plan.get("claim_token"), worker_id="public-worker")
            record = (advanced[1] or {}).get("record", advanced[1] or {})
            plan.update(record)
            if str(record.get("status", "")).lower() in {"complete", "completed"}:
                completed = ok(advanced, "advance")
                break
            if not ok(advanced, "advance"):
                break
        promotion = op("promote", f"{args.case}-promote", spec["actors"]["code"], revision_id=target_id, revision_digest=target_digest, expected_review_generation=plan.get("review_generation", (code[1] or {}).get("record", code[1] or {}).get("review_generation")), expected_head_revision_id=None, execution_store="../traceability_execution", plan_id=plan.get("plan_id"), plan_generation=plan.get("generation"))
        check("PUBLIC-EXECUTE-PROMOTE", completed and ok(promotion, "promote"), {"plan": plan, "promotion": promotion[1]})
        ordinary = workspace / "ordinary"
        shutil.copytree(ROOT / "dev_cases" / args.case / "assets" / "project", ordinary)
        (ordinary / ".deepcode" / "traceability_request.json").unlink()
        ordinary_script = "import asyncio;from pathlib import Path;from workflows.code_implementation_workflow import CodeImplementationWorkflow;asyncio.run(CodeImplementationWorkflow(require_verification=True)._verify_generated_code(Path(" + repr(str(ordinary)) + ")))"
        ordinary_result = run([sys.executable, "-c", ordinary_script], repository, env)
        processes.append(ordinary_result)
        check("PUBLIC-ORDINARY", ordinary_result["exit_code"] == 0, ordinary_result["stdout"])
    except Exception as exc:
        check("PUBLIC-FATAL", False, f"{type(exc).__name__}: {exc}")
    result = {"schema_version": "1.0", "case_id": args.case, "passed": bool(assertions) and all(item["passed"] for item in assertions), "assertions": assertions, "processes": processes}
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload)
    print(payload, end="")
    if workspace:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
