#!/usr/bin/env python3
"""Independent structural, leakage, generation, and baseline validator for v4."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS))
from evaluate_case import (  # noqa: E402
    parse_contract,
    path_allowed,
    recovery_probe,
    safe_copy,
    validate_reports,
)

CASES = ["dev_001", "dev_002", "test_001", "test_002", "test_003", "test_004", "test_005", "test_006"]
CODE_DIMENSIONS = {
    "interface_lifecycle": 15,
    "requirement_mechanism_coverage": 20,
    "analysis_evidence_integrity": 15,
    "safety_privacy_side_effects": 15,
    "recovery_honest_failure": 10,
    "testability_observability": 10,
    "maintainability_generalization": 10,
    "resource_discipline": 5,
}
GENERATORS = [
    "meta/generate_assets.py",
    "meta/generate_case_inputs.py",
    "evaluator/harness/generate_hidden_tests.py",
]
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage", ".construction", ".validation_tmp"}
CONSTRUCTION_SUFFIXES = {".pyc", ".pyo", ".patch", ".rej", ".orig", ".tmp", ".log"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate_digest(paths: list[Path]) -> str:
    hashes = {digest(path) for path in paths}
    return hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()


def production_metrics(repo: Path) -> dict[str, int]:
    physical = nonblank = code = functions = classes = branch_nodes = 0
    files = sorted((repo / "src").rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        physical += len(lines)
        nonblank += sum(bool(line.strip()) for line in lines)
        code += sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines)
        tree = ast.parse(text, filename=str(path))
        functions += sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))
        classes += sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
        branch_nodes += sum(
            isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match, ast.BoolOp, ast.IfExp, ast.comprehension))
            for node in ast.walk(tree)
        )
    return {
        "modules": len(files),
        "physical": physical,
        "nonblank": nonblank,
        "noncomment_nonblank": code,
        "functions": functions,
        "classes": classes,
        "branch_nodes": branch_nodes,
    }


def run(command: list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
        return {"exit_code": completed.returncode, "output": completed.stdout[-4000:]}
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return {"exit_code": 124, "output": output[-4000:] + "\nVALIDATOR TIMEOUT"}


def case_dir(case_id: str) -> Path:
    group = "dev_cases" if case_id.startswith("dev_") else "test_cases"
    return ROOT / group / case_id


def generated_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for case_id in CASES:
        group = "dev_cases" if case_id.startswith("dev_") else "test_cases"
        case = root / group / case_id
        files.append(case / "input.md")
        files += sorted((case / "assets" / "repository").rglob("*"))
    files.append(root / "test_cases/test_004/assets/WIREBATCH_PROTOCOL_V1.md")
    files += sorted((root / "evaluator/harness/hidden_tests").glob("*.py"))
    return [path for path in files if path.is_file()]


def generated_map(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): digest(path) for path in generated_files(root)}


def check_generators(errors: list[str], facts: dict[str, object]) -> None:
    before = generated_map(ROOT)
    with tempfile.TemporaryDirectory(prefix="v4-generator-check-") as temporary:
        temp = Path(temporary)
        copies = [temp / "first", temp / "second"]
        for destination in copies:
            shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            for generator in GENERATORS:
                result = run([sys.executable, "-B", str(destination / generator)], destination, os.environ.copy(), 30)
                if result["exit_code"] != 0:
                    errors.append(f"generator failed: {generator}: {result['output']}")
        first = generated_map(copies[0])
        second = generated_map(copies[1])
        if first != second:
            errors.append("generators are not deterministic across two clean copies")
        if before != first:
            changed = sorted(set(before) ^ set(first) | {name for name in set(before) & set(first) if before[name] != first[name]})
            errors.append("generated artifacts are stale or generator output inventory changed: " + ", ".join(changed[:20]))
        facts["generated_file_count"] = len(first)
        facts["generator_digest"] = hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()


def normalized_words(path: Path) -> set[str]:
    text = re.sub(r"```repair_contract.*?```", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return {word for word in re.findall(r"[a-z][a-z0-9_-]{3,}", text.lower()) if word not in {"repository", "repair", "public", "suite", "change", "only"}}


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def check_rubrics(errors: list[str], facts: dict[str, object]) -> None:
    result_text = (ROOT / "evaluator/rubric.md").read_text(encoding="utf-8")
    result_weights = [int(value) for value in re.findall(r"^## \d+\..*?— (\d+) points$", result_text, re.MULTILINE)]
    if result_weights != [55, 20, 15, 10] or sum(result_weights) != 100:
        errors.append(f"result rubric weights mismatch: {result_weights}")
    for required in (
        "cannot launch",
        "exceeds 600 seconds or 4 GiB",
        "target artifact is empty",
        "valid but low-quality patch",
        "Missing or malformed auxiliary metadata",
    ):
        if required not in result_text:
            errors.append(f"result rubric missing gate language: {required}")
    code_text = (ROOT / "evaluator/code_rubric.md").read_text(encoding="utf-8")
    headings = re.findall(r"^### (\d+)\. ([a-z_]+).*?— (\d+) points$", code_text, re.MULTILINE)
    observed = {identifier: int(weight) for _number, identifier, weight in headings}
    numbers = [int(number) for number, _identifier, _weight in headings]
    if numbers != list(range(1, 9)) or observed != CODE_DIMENSIONS or sum(observed.values()) != 100:
        errors.append(f"code rubric eight-ID/weight mismatch: numbers={numbers}, dimensions={observed}")
    schema_match = re.search(r'"required": \[("interface_lifecycle".*?)\]', code_text, re.DOTALL)
    for identifier in CODE_DIMENSIONS:
        if code_text.count(f'"{identifier}"') < 2:
            errors.append(f"code rubric schema omits shared ID: {identifier}")
    if schema_match is None:
        errors.append("code rubric machine-readable schema required IDs not found")
    facts["result_rubric_weights"] = result_weights
    facts["code_rubric_dimensions"] = observed


def make_probe_source(format_name: str) -> str:
    checks = '["pre_state","post_state","interruption","retry","compatibility","rollback"]'
    if format_name == "sessionarchive-jsonl-v1-v2":
        operations = "p=w/'archive.jsonl'; b=w/'archive.jsonl.bak'; t=w/'archive.jsonl.tmp'; p.write_text('{\\\"version\\\":1}\\n'); b.write_bytes(p.read_bytes()); t.write_text('{\\\"version\\\":2}\\n'); os.replace(t,p); p.read_bytes(); b.read_bytes()"
        imports = "import json,os,sys"
    elif format_name == "sqlite-tenantconfig-v1-v3":
        operations = "p=w/'config.db'\nfor i in range(3):\n c=sqlite3.connect(p); c.execute('create table if not exists t(x)'); c.execute('insert into t values (?)',(i,)); c.commit(); c.close()"
        imports = "import json,sqlite3,sys"
    else:
        operations = "payload=b'{}'; p=w/'segment-000001.bin'; p.write_bytes(struct.pack('>I',2)+payload+struct.pack('>I',zlib.crc32(payload)&0xffffffff)); n=w/'MANIFEST.next'; n.write_text('{\\\"generation\\\":1,\\\"segments\\\":[\\\"segment-000001.bin\\\"]}'); os.replace(n,w/'MANIFEST.json')"
        imports = "import json,os,struct,sys,zlib"
    return f"{imports}\nfrom pathlib import Path\nw=Path(sys.argv[-1]); {operations}\nprint(json.dumps({{'schema_version':'1.0','status':'ok','format':'{format_name}','checks':{{k:True for k in {checks}}}}}))\n"


def check_harness_negatives(errors: list[str], facts: dict[str, object]) -> None:
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="v4-harness-check-") as temporary:
        temp = Path(temporary)
        malformed = temp / "malformed"
        malformed.mkdir()
        (malformed / "input.md").write_text(
            "```repair_contract\n"
            + json.dumps(
                {
                    "schema_version": "1.0",
                    "repository": "assets/repository",
                    "allowed_paths": ["src/pkg/"],
                    "public_test_command": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
                    "network": "closed",
                    "recovery": "none",
                    "max_patch_bytes": 1,
                    "extra": True,
                }
            )
            + "\n```\n",
            encoding="utf-8",
        )
        try:
            parse_contract(malformed)
            checks["extra_contract_rejected"] = False
        except ValueError:
            checks["extra_contract_rejected"] = True
        checks["prefix_scope_rejected"] = path_allowed("src/pkg/file.py", ["src/pkg/"]) and not path_allowed("src/pkg2/file.py", ["src/pkg/"])
        source = temp / "source"
        source.mkdir()
        (source / "target").write_text("x", encoding="utf-8")
        (source / "escape").symlink_to("target")
        try:
            safe_copy(source, temp / "copy")
            checks["asset_symlink_rejected"] = False
        except ValueError:
            checks["asset_symlink_rejected"] = True
        output = temp / "reports"
        output.mkdir()
        (output / "repair_report.json").write_text('{"schema_version":"1.0"}', encoding="utf-8")
        contract = {"recovery": "none"}
        reports = validate_reports(output, contract)
        checks["malformed_target_report_rejected"] = not reports["repair_valid"]
        recovery_results: dict[str, bool] = {}
        for index, format_name in enumerate(sorted({"sessionarchive-jsonl-v1-v2", "sqlite-tenantconfig-v1-v3", "segmentstore-v1-manifest-segments"})):
            fixture = temp / f"recovery-{index}"
            repo = fixture / "repo"
            artifact = repo / "src/pkg/probe.py"
            artifact.parent.mkdir(parents=True)
            work = fixture / "work"
            work.mkdir(parents=True)
            home = fixture / "home"
            home.mkdir()
            process_temp = fixture / "tmp"
            process_temp.mkdir()
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(home),
                "TMPDIR": str(process_temp),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(repo / "src"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            report = {
                "format": format_name,
                "artifact": "src/pkg/probe.py",
                "command": ["{python}", "{artifact}", "--self-test", "{work}"],
            }
            artifact.write_text(
                "import json\nprint(json.dumps({'schema_version':'1.0','status':'ok','format':'"
                + format_name
                + "','checks':{k:True for k in ['pre_state','post_state','interruption','retry','compatibility','rollback']}}))\n",
                encoding="utf-8",
            )
            fake = recovery_probe(report, repo, work, env, ["src/pkg/"], ["src/pkg/probe.py"], fixture / "audit-fake")
            artifact.write_text(make_probe_source(format_name), encoding="utf-8")
            meaningful = recovery_probe(report, repo, work, env, ["src/pkg/"], ["src/pkg/probe.py"], fixture / "audit-real")
            recovery_results[format_name] = not fake["valid"] and meaningful["valid"]
        checks["recovery_positive_negative_paths"] = all(recovery_results.values())
        facts["recovery_harness_formats"] = recovery_results
    for name, passed in checks.items():
        if not passed:
            errors.append(f"harness negative check failed: {name}")
    facts["harness_negative_checks"] = checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    errors: list[str] = []
    facts: dict[str, object] = {}
    inputs = sorted((ROOT / "input").glob("*.md"))
    dev = sorted((ROOT / "dev_cases").glob("dev_*"))
    tests = sorted((ROOT / "test_cases").glob("test_*"))
    if [path.name for path in inputs] != ["01_task_goal.md", "02_interface_and_delivery.md", "03_requirements_and_constraints.md", "04_resources.md"]:
        errors.append("builder input inventory mismatch")
    if [path.name for path in dev] != ["dev_001", "dev_002"] or [path.name for path in tests] != [f"test_{index:03d}" for index in range(1, 7)]:
        errors.append("case count or numbering mismatch")
    root_entries = {path.name for path in ROOT.iterdir()}
    expected_root = {"README.md", "input", "dev_cases", "test_cases", "evaluator", "meta"}
    if root_entries != expected_root:
        errors.append(f"root inventory mismatch: extra={sorted(root_entries - expected_root)}, missing={sorted(expected_root - root_entries)}")
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if path.name in CACHE_NAMES or path.suffix in CONSTRUCTION_SUFFIXES:
            errors.append(f"cache/construction output present: {relative}")
        if path.is_symlink():
            errors.append(f"symlink present in final artifact: {relative}")
            continue
        mode = path.stat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            errors.append(f"special file present: {relative}")
    contracts: dict[str, dict[str, object]] = {}
    metrics: dict[str, dict[str, int]] = {}
    for case in dev + tests:
        try:
            contract = parse_contract(case)
            contracts[case.name] = contract
        except Exception as exc:
            errors.append(f"{case.name} contract: {exc}")
            continue
        if {path.name for path in case.iterdir()} != {"input.md", "assets"}:
            errors.append(f"{case.name} contains non-runtime case files")
        repo = case / str(contract["repository"])
        for required in ("README.md", "pyproject.toml", "src", "tests"):
            if not (repo / required).exists():
                errors.append(f"{case.name} missing repository/{required}")
        project_text = (repo / "pyproject.toml").read_text(encoding="utf-8")
        if "[project]" not in project_text or "dependencies = []" not in project_text or "requires-python = \">=3.10\"" not in project_text:
            errors.append(f"{case.name} pyproject is not dependency-free Python 3.10+")
        metrics[case.name] = production_metrics(repo)
        if case.name.startswith("test_") and not 250 <= metrics[case.name]["physical"] <= 1200:
            errors.append(f"{case.name} physical production lines outside 250..1200: {metrics[case.name]['physical']}")
        if metrics[case.name]["modules"] < 6 or metrics[case.name]["functions"] < 15 or metrics[case.name]["branch_nodes"] < 10:
            errors.append(f"{case.name} production structure is too small: {metrics[case.name]}")
    facts["production_metrics"] = metrics
    for path in sorted(ROOT.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            errors.append(f"python syntax {path.relative_to(ROOT)}: {exc}")
    text_files = [path for path in ROOT.rglob("*") if path.is_file() and path.suffix in {".py", ".md", ".toml", ".json"}]
    for path in text_files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            errors.append(f"non-UTF8 {path.relative_to(ROOT)}: {exc}")
            continue
        markers = ["TO" + "DO", "FIX" + "ME", "PLACE" + "HOLDER"]
        if path != Path(__file__) and any(marker in text for marker in markers):
            errors.append(f"unfinished marker in {path.relative_to(ROOT)}")
        if ("dev_cases" in path.parts or "test_cases" in path.parts) and re.search(r"\bBUG\s*:", text, re.IGNORECASE):
            errors.append(f"case oracle-style BUG marker in {path.relative_to(ROOT)}")
    public_files = [path for path in inputs + [ROOT / "README.md"] if path.exists()]
    for case in dev:
        public_files += [path for path in case.rglob("*") if path.is_file()]
    hidden_runtime = [path for case in tests for path in case.rglob("*") if path.is_file()]
    public_hashes = {digest(path) for path in public_files}
    hidden_hashes = {digest(path) for path in hidden_runtime}
    overlap = public_hashes & hidden_hashes
    if overlap:
        errors.append(f"public/hidden exact file-hash overlap: {len(overlap)}")
    public_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in public_files)
    hidden_packages = []
    for case in tests:
        hidden_packages.extend(path.name for path in (case / "assets/repository/src").iterdir() if path.is_dir())
    leaked = [name for name in hidden_packages if name in public_text]
    if leaked:
        errors.append("hidden package names leaked publicly: " + ", ".join(sorted(leaked)))
    facts["public_partition_sha256"] = aggregate_digest(public_files)
    facts["hidden_partition_sha256"] = aggregate_digest(hidden_runtime)
    words = {case.name: normalized_words(case / "input.md") for case in dev + tests}
    similarities: dict[str, float] = {}
    case_paths = dev + tests
    for index, left in enumerate(case_paths):
        for right in case_paths[index + 1 :]:
            score = round(jaccard(words[left.name], words[right.name]), 4)
            similarities[f"{left.name}:{right.name}"] = score
            if score >= 0.62:
                errors.append(f"case inputs are insufficiently distinct: {left.name}/{right.name}={score}")
    facts["input_word_jaccard"] = similarities
    hidden_method_sets: dict[str, list[str]] = {}
    for case_id in CASES:
        tree = ast.parse((ROOT / "evaluator/harness/hidden_tests" / f"{case_id}.py").read_text(encoding="utf-8"))
        methods = sorted(node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
        hidden_method_sets[case_id] = methods
        if len(methods) < 3:
            errors.append(f"{case_id} has fewer than three independent evaluator tests")
    if len({tuple(methods) for methods in hidden_method_sets.values()}) != len(CASES):
        errors.append("two cases share the same hidden test-method structure")
    facts["hidden_test_methods"] = hidden_method_sets
    check_rubrics(errors, facts)
    check_generators(errors, facts)
    check_harness_negatives(errors, facts)
    construction_text = (ROOT / "meta/construction_report.md").read_text(encoding="utf-8")
    for field in ("public_partition_sha256", "hidden_partition_sha256"):
        if str(facts[field]) not in construction_text:
            errors.append(f"construction metadata does not contain current {field}")
    if args.run_tests:
        baseline: dict[str, dict[str, int]] = {}
        with tempfile.TemporaryDirectory(prefix="v4-validation-") as temporary:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(Path(temporary) / "home"),
                "TMPDIR": str(Path(temporary) / "tmp"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            Path(env["HOME"]).mkdir()
            Path(env["TMPDIR"]).mkdir()
            for case in dev + tests:
                repo = case / str(contracts[case.name]["repository"])
                public_env = {**env, "PYTHONPATH": str(repo / "src")}
                public = run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], repo, public_env, 30)
                hidden = run([sys.executable, "-B", str(ROOT / "evaluator/harness/hidden_tests" / f"{case.name}.py")], repo, public_env, 60)
                baseline[case.name] = {"public_exit": int(public["exit_code"]), "hidden_exit": int(hidden["exit_code"])}
                if public["exit_code"] != 0:
                    errors.append(f"{case.name} pristine public suite fails: {public['output']}")
                if hidden["exit_code"] == 0:
                    errors.append(f"{case.name} pristine hidden suite unexpectedly passes")
                if hidden["exit_code"] == 124:
                    errors.append(f"{case.name} pristine hidden suite timed out instead of exposing a bounded failure")
        facts["baseline"] = baseline
    result = {
        "valid": not errors,
        "errors": errors,
        "facts": facts,
        "counts": {
            "builder_inputs": len(inputs),
            "dev_cases": len(dev),
            "hidden_cases": len(tests),
            "python_files": len(list(ROOT.rglob("*.py"))),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
