#!/usr/bin/env python3
"""Deterministic construction, leakage, and structure validator."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

EXPECTED_INPUTS = {
    "01_task_goal.md",
    "02_interface_and_delivery.md",
    "03_requirements_and_constraints.md",
    "04_resources.md",
}
EXPECTED_DEV = {"dev_001", "dev_002"}
EXPECTED_TEST = {f"test_{index:03d}" for index in range(1, 7)}
TOOLCHAIN = "leanprover/lean4:v4.19.0"
ORACLE_WORDS = re.compile(r"(?:answer|oracle|expected[_-]?output|reference[_-]?solution|golden)", re.I)
DECL_KIND = re.compile(r"^(theorem|lemma|def|class|structure|inductive|instance)\b")
CODE_DIMENSIONS = [
    ("interface_lifecycle", 15),
    ("requirement_mechanism_coverage", 20),
    ("analysis_evidence_integrity", 15),
    ("safety_privacy_side_effects", 15),
    ("recovery_honest_failure", 10),
    ("testability_observability", 10),
    ("maintainability_generalization", 10),
    ("resource_discipline", 5),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strip_comments(text: str) -> str:
    text = re.sub(r"/-.*?-/", "", text, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def lean_sloc(repo: Path) -> int:
    total = 0
    for path in repo.rglob("*.lean"):
        clean = strip_comments(path.read_text(encoding="utf-8"))
        total += sum(1 for line in clean.splitlines() if line.strip())
    return total


def structure_fingerprint(repo: Path) -> str:
    parts: list[str] = []
    for path in sorted(repo.rglob("*.lean")):
        kinds: list[str] = []
        for line in strip_comments(path.read_text(encoding="utf-8")).splitlines():
            match = DECL_KIND.match(line)
            if match:
                kinds.append(match.group(1))
        parts.append(f"{path.relative_to(repo)}:{','.join(kinds)}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def case_tree_hash(case: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in case.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(case)).encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def parse_hash_manifest(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split("  ", 1)
        result[relative] = digest
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.benchmark_root.resolve()
    errors: list[str] = []
    observations: dict[str, object] = {}
    expected_root = {"README.md", "input", "dev_cases", "test_cases", "evaluator", "meta"}
    actual_root = {path.name for path in root.iterdir()}
    if actual_root != expected_root:
        errors.append(
            "root inventory mismatch: "
            f"extra={sorted(actual_root - expected_root)}, "
            f"missing={sorted(expected_root - actual_root)}"
        )

    actual_inputs = {path.name for path in (root / "input").glob("*.md")}
    if actual_inputs != EXPECTED_INPUTS:
        errors.append(f"builder input set mismatch: {sorted(actual_inputs)}")
    dev_names = {path.name for path in (root / "dev_cases").iterdir() if path.is_dir()}
    test_names = {path.name for path in (root / "test_cases").iterdir() if path.is_dir()}
    if dev_names != EXPECTED_DEV:
        errors.append(f"development case set mismatch: {sorted(dev_names)}")
    if test_names != EXPECTED_TEST:
        errors.append(f"hidden case set mismatch: {sorted(test_names)}")

    policy_path = root / "evaluator" / "harness" / "case_policy.json"
    policies = json.loads(policy_path.read_text(encoding="utf-8"))
    if set(policies) != EXPECTED_DEV | EXPECTED_TEST:
        errors.append("case policy keys do not match the eight cases")

    case_records: dict[str, dict[str, object]] = {}
    runtime_files: list[Path] = []
    lean_hashes: dict[str, list[str]] = {"dev": [], "test": []}
    structures: dict[str, str] = {}
    tree_hashes: dict[str, str] = {}
    for group_name, names in (("dev", EXPECTED_DEV), ("test", EXPECTED_TEST)):
        group_dir = root / ("dev_cases" if group_name == "dev" else "test_cases")
        for name in sorted(names):
            case = group_dir / name
            children = {path.name for path in case.iterdir()}
            if children != {"input.md", "assets"}:
                errors.append(f"{name}: case root must contain only input.md and assets")
            if any(ORACLE_WORDS.search(path.name) for path in case.rglob("*")):
                errors.append(f"{name}: oracle-like filename found")
            repo = case / "assets" / "repository"
            lean_files = sorted(repo.rglob("*.lean"))
            sloc = lean_sloc(repo)
            if not 50 <= sloc <= 250:
                errors.append(f"{name}: Lean SLOC {sloc} is outside 50..250")
            if len(lean_files) < 2:
                errors.append(f"{name}: fewer than two Lean modules")
            toolchain = (repo / "lean-toolchain").read_text(encoding="utf-8").strip()
            if toolchain != TOOLCHAIN:
                errors.append(f"{name}: unexpected toolchain {toolchain}")
            manifest = json.loads((repo / "lake-manifest.json").read_text(encoding="utf-8"))
            if manifest.get("packages") != []:
                errors.append(f"{name}: external package dependency present")
            lakefile = (repo / "lakefile.toml").read_text(encoding="utf-8")
            if "require " in lakefile or "git =" in lakefile:
                errors.append(f"{name}: remote dependency syntax present")
            input_text = (case / "input.md").read_text(encoding="utf-8")
            for required in policies[name].get("input_must_contain", []):
                if required not in input_text:
                    errors.append(f"{name}: public input omits policy phrase {required!r}")
            for changed in policies[name]["changed_paths"]:
                if changed not in input_text:
                    errors.append(f"{name}: changed-path policy {changed!r} absent from input")
            for target in policies[name].get("targets", []):
                for token in target.get("required_tokens", []):
                    if token not in input_text:
                        errors.append(f"{name}: required proof-method token {token!r} absent from input")
            for path in [case / "input.md", *sorted((case / "assets").rglob("*"))]:
                if path.is_file():
                    runtime_files.append(path)
            for path in lean_files:
                lean_hashes[group_name].append(sha256(path))
            structures[name] = structure_fingerprint(repo)
            tree_hashes[name] = case_tree_hash(case)
            case_records[name] = {
                "lean_sloc": sloc,
                "lean_modules": len(lean_files),
                "tree_sha256": tree_hashes[name],
                "structure_sha256": structures[name],
            }

    duplicate_lean = set(lean_hashes["dev"]) & set(lean_hashes["test"])
    if duplicate_lean:
        errors.append("public and hidden cases contain byte-identical Lean modules")
    if len(set(tree_hashes.values())) != len(tree_hashes):
        errors.append("two cases have identical complete-tree hashes")
    for hidden in EXPECTED_TEST:
        if structures[hidden] in {structures[dev] for dev in EXPECTED_DEV}:
            errors.append(f"{hidden}: declaration structure duplicates a development case")

    public_paths = [root / "README.md", *sorted((root / "input").glob("*.md"))]
    public_paths += sorted((root / "dev_cases").rglob("*"))
    public_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in public_paths
        if path.is_file()
    )
    leaked_targets = []
    for case_name in EXPECTED_TEST:
        for target in policies[case_name]["targets"]:
            if target["name"] in public_text:
                leaked_targets.append(target["name"])
    if leaked_targets:
        errors.append(f"hidden fully qualified targets leaked into public material: {sorted(leaked_targets)}")

    rubric = (root / "evaluator" / "rubric.md").read_text(encoding="utf-8")
    result_weights = [int(value) for value in re.findall(r"^##\s+\d+\..*?—\s*(\d+) points$", rubric, re.M)]
    if sum(result_weights) != 100 or not 4 <= len(result_weights) <= 7:
        errors.append(f"result rubric weights invalid: {result_weights}")
    if "final artifact" not in rubric.lower() or "implementation" not in rubric.lower():
        errors.append("result rubric does not state its final-artifact evidence boundary")
    code_rubric = (root / "evaluator" / "code_rubric.md").read_text(encoding="utf-8")
    code_weights = [int(value) for value in re.findall(r"^###\s+\d+\..*?—\s*(\d+) points$", code_rubric, re.M)]
    if len(code_weights) != 8 or sum(code_weights) != 100:
        errors.append(f"code rubric must have eight dimensions totaling 100: {code_weights}")
    code_headings = [
        (name, int(weight))
        for name, weight in re.findall(
            r"^###\s+\d+\.\s+`([^`]+)`.*?—\s*(\d+) points$",
            code_rubric,
            re.M,
        )
    ]
    if code_headings != CODE_DIMENSIONS:
        errors.append(f"code rubric IDs/maxima do not match the shared schema: {code_headings}")
    code_json_dimensions = [
        (name, int(weight))
        for name, weight in re.findall(
            r'^\s{4}"([a-z_]+)": \{"score": 0, "max": (\d+), "evidence":',
            code_rubric,
            re.M,
        )
    ]
    if code_json_dimensions != CODE_DIMENSIONS:
        errors.append(f"code rubric JSON keys/maxima do not match the shared schema: {code_json_dimensions}")

    interface_text = (root / "input" / "02_interface_and_delivery.md").read_text(encoding="utf-8")
    resource_text = (root / "input" / "04_resources.md").read_text(encoding="utf-8")
    for phrase in ("Installation is a no-op", "exactly one Markdown input", "finite nonnegative"):
        if phrase not in interface_text:
            errors.append(f"interface contract is missing required exact-schema phrase {phrase!r}")
    for phrase in (
        "https://gateway.example.com/v1/responses",
        "Authorization: Bearer $GATEWAY_API_KEY",
        '"reasoning":{"effort":"medium"}',
    ):
        if phrase not in resource_text:
            errors.append(f"resource contract is missing invocation detail {phrase!r}")

    provenance = "\n".join(
        (root / "meta" / name).read_text(encoding="utf-8")
        for name in ("source_repo_analysis.md", "construction_report.md", "reference_baseline.md")
    ).lower()
    if "synthetic" not in provenance or "provenance" not in provenance or "lean 4.19.0" not in provenance:
        errors.append("metadata lacks synthetic provenance and pinned-toolchain statements")

    manifest_path = root / "meta" / "runtime_asset_hashes.sha256"
    if not manifest_path.is_file():
        errors.append("runtime asset hash manifest is missing")
    else:
        recorded = parse_hash_manifest(manifest_path)
        expected = {str(path.relative_to(root)): sha256(path) for path in runtime_files}
        if recorded != expected:
            errors.append("runtime asset hash manifest does not match case inputs/assets")

    observations["case_records"] = case_records
    observations["result_rubric_weights"] = result_weights
    observations["code_rubric_weights"] = code_weights
    observations["public_hidden_identical_lean_hashes"] = len(duplicate_lean)
    observations["hidden_target_leaks"] = leaked_targets
    observations["runtime_file_count"] = len(runtime_files)
    forbidden_generated = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (
            path.name == ".construction"
            or path.name == "__pycache__"
            or path.name == ".lake"
            or path.suffix in {".pyc", ".olean", ".ilean", ".o", ".a", ".zst"}
        ):
            forbidden_generated.append(str(relative))
    if forbidden_generated:
        errors.append(f"generated/cache/toolchain artifacts remain: {sorted(forbidden_generated)}")
    observations["forbidden_generated_artifacts"] = sorted(forbidden_generated)
    payload = {"ok": not errors, "errors": errors, "observations": observations}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
