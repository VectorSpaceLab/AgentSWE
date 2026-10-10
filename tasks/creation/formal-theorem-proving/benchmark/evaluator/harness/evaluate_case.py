#!/usr/bin/env python3
"""Evaluator-owned final-artifact checks for Formal Lean v4."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "case_policy.json"
DECL_START = re.compile(r"^(?:theorem|lemma|def|class|structure|inductive|instance)\s+([A-Za-z_][A-Za-z0-9_']*)\b")
BOUNDARY = re.compile(r"^(?:theorem|lemma|def|class|structure|inductive|instance|namespace|section|end)\b")
GLOBAL_FORBIDDEN = (
    "sorry", "admit", "unsafe", "axiom", "opaque", "native_decide",
    "by_contra!", "set_option", "exact?", "apply?", "simp?", "aesop?",
    "run_tac", "run_cmd", "implemented_by",
)
PROVIDER_KEYS = {"gateway_text", "gateway_image", "serper", "web_retrieval"}
PROOF_FIELDS = {
    "schema_version", "status", "targets", "files_changed",
    "validation", "diagnosis", "counterexample",
}
RUN_FIELDS = {
    "status", "artifact_paths", "errors", "runtime_seconds",
    "peak_memory_mb", "provider_counts",
}
SAFE_DECL = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")


def load_policies(path: Path = POLICY_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("case policy must be a JSON object")
    return data


def memory_limit() -> None:
    limit = 4 * 1024**3
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_command(cmd: list[str], cwd: Path, home: Path, timeout: int = 600) -> dict[str, Any]:
    started = time.monotonic()
    temp_dir = home / "tmp"
    cache_dir = home / "cache"
    temp_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(temp_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            preexec_fn=memory_limit,
            env=env,
        )
        return {
            "command": cmd,
            "exit_code": proc.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "output": proc.stdout[-30000:],
        }
    except FileNotFoundError as exc:
        return {
            "infrastructure_error": True,
            "command": cmd,
            "exit_code": 127,
            "seconds": round(time.monotonic() - started, 3),
            "output": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return {
            "command": cmd,
            "exit_code": 124,
            "seconds": round(time.monotonic() - started, 3),
            "output": output[-30000:],
        }


def load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size > 2_000_000:
        return None
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def is_plain_int(value: Any) -> bool:
    return type(value) is int


def is_finite_nonnegative_number(value: Any) -> bool:
    if type(value) is int:
        return value >= 0
    return type(value) is float and math.isfinite(value) and value >= 0


def valid_command_field(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_validation(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict)
            and set(item) == {"command", "exit_code", "observation"}
            and valid_command_field(item.get("command"))
            and is_plain_int(item.get("exit_code"))
            and isinstance(item.get("observation"), str)
            and bool(item["observation"].strip())
            for item in value
        )
    )


def strip_lean_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    block_depth = 0
    in_string = False
    while i < len(text):
        pair = text[i:i + 2]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                i += 2
            elif pair == "-/":
                block_depth -= 1
                i += 2
            else:
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            continue
        if not in_string and pair == "/-":
            block_depth = 1
            i += 2
            continue
        if not in_string and pair == "--":
            newline = text.find("\n", i)
            if newline == -1:
                break
            out.append("\n")
            i = newline + 1
            continue
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_string = not in_string
            out.append('""')
        elif in_string:
            if ch == "\n":
                out.append("\n")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def declaration_proofs(text: str, leaf_name: str) -> list[dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^(?:theorem|lemma)\s+{re.escape(leaf_name)}\b")
    results: list[dict[str, Any]] = []
    for start, line in enumerate(lines):
        if not pattern.match(line):
            continue
        marker_line = None
        marker_col = None
        for index in range(start, len(lines)):
            current = lines[index]
            marker = current.find(":= by")
            if marker >= 0:
                marker_line = index
                marker_col = marker + len(":= by")
                break
            if index > start and BOUNDARY.match(current):
                break
        if marker_line is None or marker_col is None:
            continue
        end = len(lines)
        for index in range(marker_line + 1, len(lines)):
            if BOUNDARY.match(lines[index]):
                end = index
                break
        inline = lines[marker_line][marker_col:]
        body = inline + "".join(lines[marker_line + 1:end])
        results.append({
            "start_line": start,
            "marker_line": marker_line,
            "marker_col": marker_col,
            "end_line": end,
            "body": body,
        })
    return results


def declaration_proof(text: str, leaf_name: str) -> dict[str, Any]:
    matches = declaration_proofs(text, leaf_name)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one `:= by` proof for {leaf_name}, found {len(matches)}")
    return matches[0]


def mask_target_proofs(text: str, leaves: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    spans = []
    for leaf in leaves:
        proof = declaration_proof(text, leaf)
        spans.append((proof["marker_line"], proof["marker_col"], proof["end_line"], leaf))
    for marker_line, marker_col, end_line, leaf in sorted(spans, reverse=True):
        newline = "\n" if lines[marker_line].endswith("\n") else ""
        lines[marker_line:end_line] = [lines[marker_line][:marker_col] + f" <MASKED:{leaf}>" + newline]
    return "".join(lines)


def proof_noncomment_lines(body: str) -> int:
    clean = strip_lean_comments(body)
    return sum(1 for line in clean.splitlines() if line.strip())


def contains_token(text: str, token: str) -> bool:
    if token.endswith("?"):
        return token in text
    return re.search(rf"(?<![A-Za-z0-9_']){re.escape(token)}(?![A-Za-z0-9_'])", text) is not None


def safe_patch_text(text: str) -> list[str]:
    errors: list[str] = []
    if "\x00" in text or "GIT binary patch" in text or "Binary files " in text:
        errors.append("binary patch content is forbidden")
    if re.search(r"(?m)^(?:old mode|new mode|deleted file mode|rename from|rename to|copy from|copy to|Subproject commit)", text):
        errors.append("mode, rename, copy, deletion-mode, or submodule patch metadata is forbidden")
    if re.search(r"(?m)^new file mode (?!100644$)", text):
        errors.append("new files must be regular mode 100644")
    for side, raw in re.findall(r"(?m)^diff --git ([^ ]+) ([^ ]+)$", text):
        for label in (side, raw):
            if not label.startswith(("a/", "b/")):
                errors.append(f"non-relative diff path: {label}")
                continue
            path = PurePosixPath(label[2:])
            if path.is_absolute() or ".." in path.parts or not path.parts:
                errors.append(f"unsafe diff path: {label}")
    for label in re.findall(r"(?m)^(?:---|\+\+\+)\s+([^\t\n]+)", text):
        if label == "/dev/null":
            continue
        if not label.startswith(("a/", "b/")):
            errors.append(f"non-relative patch marker path: {label}")
            continue
        path = PurePosixPath(label[2:])
        if path.is_absolute() or ".." in path.parts or not path.parts:
            errors.append(f"unsafe patch marker path: {label}")
    return errors


def validation_has(report: dict[str, Any], command_parts: list[str]) -> bool:
    validation = report.get("validation")
    if not isinstance(validation, list):
        return False
    for item in validation:
        if not isinstance(item, dict) or not is_plain_int(item.get("exit_code")):
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        if tokens:
            tokens[0] = Path(tokens[0]).name
        expected = command_parts.copy()
        expected[0] = Path(expected[0]).name
        if tokens == expected and item["exit_code"] == 0 and isinstance(item.get("observation"), str):
            return True
    return False


def write_result(path: Path, result: dict[str, Any]) -> None:
    infrastructure = result.get('infrastructure_errors', [])
    result['evaluation_state'] = 'infrastructure_error' if infrastructure else ('scoreable' if result.get('validity_gate') else 'fatal_zero')
    if infrastructure:
        result['validity_gate'] = False
    result['fatal_errors'] = result.get('errors', []) if not infrastructure else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def dependency_checker_source(import_module: str, checks: list[tuple[str, str]]) -> str:
    lines = [
        f"import {import_module}",
        "import Lean",
        "",
        "namespace FormalEvaluator.DependencyAudit",
        "",
        "open Lean Elab Command",
        "",
        "private partial def eraseUnusedLets (expr : Expr) : Expr :=",
        "  if let some (args, _, _, value, body) := expr.letFunAppArgs? then",
        "    mkAppN (eraseUnusedLets (body.instantiate1 value)) (args.map eraseUnusedLets)",
        "  else",
        "    match expr with",
        "    | .letE _ _ value body _ => eraseUnusedLets (body.instantiate1 value)",
        "    | .app fn arg => .app (eraseUnusedLets fn) (eraseUnusedLets arg)",
        "    | .lam name type body info => .lam name (eraseUnusedLets type) (eraseUnusedLets body) info",
        "    | .forallE name type body info => .forallE name (eraseUnusedLets type) (eraseUnusedLets body) info",
        "    | .mdata data body => .mdata data (eraseUnusedLets body)",
        "    | .proj typeName index body => .proj typeName index (eraseUnusedLets body)",
        "    | expr => expr",
        "",
        "private def checkDependency (target dependency : Name) : CommandElabM Unit := do",
        "  let env ← getEnv",
        "  let some targetInfo := env.find? target",
        "    | throwError m!\"target declaration missing: {target}\"",
        "  let some dependencyInfo := env.find? dependency",
        "    | throwError m!\"required declaration missing: {dependency}\"",
        "  let enforce := match dependencyInfo with | .thmInfo _ => true | _ => false",
        "  if enforce then",
        "    let some value := targetInfo.value?",
        "      | throwError m!\"target proof value missing: {target}\"",
        "    let used := (eraseUnusedLets value).getUsedConstants",
        "    unless used.contains dependency do",
        "      throwError m!\"required theorem is not retained by the proof: {target} -> {dependency}\"",
        "",
        "run_cmd do",
    ]
    lines.extend(f"  checkDependency `{target} `{dependency}" for target, dependency in checks)
    lines.extend(["", "end FormalEvaluator.DependencyAudit", ""])
    return "\n".join(lines)


def run_dependency_checker(
    repo: Path,
    home: Path,
    import_module: str,
    checks: list[tuple[str, str]],
) -> dict[str, Any] | None:
    if not checks:
        return None
    checker = repo / "FormalEvaluatorDependencies.lean"
    checker.write_text(dependency_checker_source(import_module, checks), encoding="utf-8")
    try:
        return run_command(["lake", "env", "lean", checker.name], repo, home, 600)
    finally:
        checker.unlink(missing_ok=True)


def evaluate(case_dir: Path, output_dir: Path, result_path: Path) -> int:
    policies = load_policies()
    key = case_dir.name
    result: dict[str, Any] = {"case": key, "validity_gate": False, "errors": [], 'quality_errors': [], 'infrastructure_errors': []}
    errors: list[str] = result["errors"]
    quality = result['quality_errors']
    infrastructure = result['infrastructure_errors']
    if key not in policies:
        infrastructure.append("unknown trusted case policy")
        write_result(result_path, result)
        return 2
    policy = policies[key]
    patch_path = output_dir / "solution.patch"
    proof_path = output_dir / "proof_report.json"
    run_path = output_dir / "run_report.json"
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in output_dir.rglob('*')):
        errors.append('unsafe output symlink or special object')
        write_result(result_path, result)
        return 2
    proof = load_json_object(proof_path)
    run_report = load_json_object(run_path)

    if not patch_path.is_file():
        errors.append("solution.patch missing")
    if proof is None:
        errors.append("proof_report.json has the wrong object shape")
    elif set(proof) != PROOF_FIELDS:
        quality.append('proof_report.json fields differ from the requested schema')
    if run_report is None or set(run_report) != RUN_FIELDS:
        quality.append("run_report.json has the wrong object shape")
        run_report = run_report or {}
    if errors:
        write_result(result_path, result)
        return 2

    assert proof is not None and run_report is not None
    result["report_checks"] = {}
    checks = result["report_checks"]
    checks["schema_version"] = proof.get("schema_version") == "1.1"
    checks["status"] = proof.get("status") == policy["mode"]
    checks["targets"] = proof.get("targets") == [target["name"] for target in policy["targets"]]
    checks["diagnosis"] = isinstance(proof.get("diagnosis"), str) and bool(proof["diagnosis"].strip())
    checks["validation_schema"] = valid_validation(proof.get("validation"))
    checks["run_status"] = run_report.get("status") == "success"
    checks["run_types"] = (
        isinstance(run_report.get("artifact_paths"), list)
        and isinstance(run_report.get("errors"), list)
        and all(isinstance(item, str) for item in run_report["errors"])
        and run_report["errors"] == []
        and is_finite_nonnegative_number(run_report.get("runtime_seconds"))
        and is_finite_nonnegative_number(run_report.get("peak_memory_mb"))
    )
    counts = run_report.get("provider_counts")
    checks["provider_counts"] = (
        isinstance(counts, dict)
        and set(counts) == PROVIDER_KEYS
        and all(is_plain_int(counts[name]) and counts[name] >= 0 for name in PROVIDER_KEYS)
        and counts["serper"] == 0
        and counts["web_retrieval"] == 0
    )
    checks["artifact_paths"] = run_report.get("artifact_paths") == [
        "solution.patch", "proof_report.json", "run_report.json"
    ]
    for name, ok in checks.items():
        if not ok:
            quality.append(f"report check failed: {name}")
    if isinstance(counts, dict) and any(is_plain_int(counts.get(k)) and counts[k] > 0 for k in ('serper', 'web_retrieval')):
        errors.append('closed-corpus retrieval is reported as nonzero')

    if patch_path.stat().st_size == 0 or patch_path.stat().st_size > 2_000_000:
        errors.append("patch size is outside the permitted range")
        patch_text = ""
    else:
        try:
            patch_text = patch_path.read_text(encoding="utf-8")
        except Exception as exc:
            patch_text = ""
            errors.append(f"patch is not valid UTF-8: {exc}")
    errors.extend(safe_patch_text(patch_text))
    if errors:
        write_result(result_path, result)
        return 2

    with tempfile.TemporaryDirectory(prefix="formal-v4-eval-") as temp:
        temp_root = Path(temp)
        repo = temp_root / "repo"
        home = temp_root / "home"
        home.mkdir()
        health_file = temp_root / 'TrustedHealth.lean'
        health_file.write_text('import Lean\nexample : Nat.succ 0 = 1 := by rfl\n')
        health = [run_command(['lean', '--version'], temp_root, home, 20),
                  run_command(['lake', '--version'], temp_root, home, 20),
                  run_command(['lean', str(health_file)], temp_root, home, 30)]
        result['toolchain_health'] = health
        if any(x['exit_code'] != 0 for x in health) or '4.19.0' not in health[0]['output']:
            infrastructure.append('trusted Lean 4.19.0 toolchain health check failed before Candidate proof compilation')
            write_result(result_path, result)
            return 2
        shutil.copytree(case_dir / "assets" / "repository", repo, symlinks=True)
        for cache in repo.rglob(".lake"):
            if cache.is_dir():
                shutil.rmtree(cache)
        baseline = {
            str(path.relative_to(repo)): path.read_text(encoding="utf-8")
            for path in sorted(repo.rglob("*.lean"))
        }
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "eval@example.invalid"],
            ["git", "config", "user.name", "Formal v4 Evaluator"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "pristine fixture"],
        ):
            observed = run_command(command, repo, home, 30)
            if observed["exit_code"] != 0:
                infrastructure.append("failed to initialize isolated Git fixture")
                result["git_init"] = observed
                write_result(result_path, result)
                return 2

        patch_check = run_command(["git", "apply", "--check", str(patch_path)], repo, home, 30)
        result["patch_check"] = patch_check
        if patch_check["exit_code"] != 0:
            errors.append("patch does not apply exactly once to the pristine fixture")
        else:
            applied = run_command(["git", "apply", str(patch_path)], repo, home, 30)
            if applied["exit_code"] != 0:
                errors.append("patch application failed after a successful check")

        diff_names = run_command(["git", "diff", "--name-only"], repo, home, 30)["output"].splitlines()
        new_names = run_command(["git", "ls-files", "--others", "--exclude-standard"], repo, home, 30)["output"].splitlines()
        changed = sorted(set(path for path in diff_names + new_names if path))
        expected_changed = sorted(policy["changed_paths"])
        result["scope"] = {"changed": changed, "expected": expected_changed, "ok": changed == expected_changed}
        if changed != expected_changed:
            errors.append("changed paths do not exactly match the public case scope")
        if proof.get("files_changed") != changed:
            quality.append("proof_report files_changed does not match the applied patch")
        for relative in changed:
            pure = PurePosixPath(relative)
            target = repo / relative
            if pure.is_absolute() or ".." in pure.parts or target.is_symlink() or not target.is_file():
                errors.append(f"changed path is unsafe or not a regular file: {relative}")

        targets_by_file: dict[str, list[str]] = {}
        if policy["mode"] == "proved":
            for target in policy["targets"]:
                targets_by_file.setdefault(target["file"], []).append(target["leaf"])
        integrity: dict[str, bool] = {}
        for relative, before in baseline.items():
            after_path = repo / relative
            if not after_path.is_file():
                integrity[relative] = False
                continue
            after = after_path.read_text(encoding="utf-8")
            leaves = targets_by_file.get(relative, [])
            try:
                integrity[relative] = mask_target_proofs(before, leaves) == mask_target_proofs(after, leaves)
            except ValueError:
                integrity[relative] = False
        result["source_integrity"] = integrity
        if not all(integrity.values()):
            errors.append("imports, declaration headers, or non-target Lean source changed")
        if errors:
            write_result(result_path, result)
            return 2

        target_results: dict[str, Any] = {}
        positions: dict[str, tuple[str, int]] = {}
        for target in policy["targets"] if policy["mode"] == "proved" else []:
            path = repo / target["file"]
            try:
                source = path.read_text(encoding="utf-8")
                extracted = declaration_proof(source, target["leaf"])
                clean_body = strip_lean_comments(extracted["body"])
                line_count = proof_noncomment_lines(extracted["body"])
                required = {name: name in clean_body for name in target.get("required_refs", [])}
                forbidden = {
                    token: contains_token(clean_body, token)
                    for token in target.get("forbidden_tokens", [])
                }
                required_tokens = {
                    token: contains_token(clean_body, token)
                    for token in target.get("required_tokens", [])
                }
                target_results[target["name"]] = {
                    "file": target["file"],
                    "proof_lines": line_count,
                    "max_proof_lines": target["max_proof_lines"],
                    "required_refs": required,
                    "required_tokens": required_tokens,
                    "forbidden_tokens_present": [name for name, present in forbidden.items() if present],
                }
                positions[target["name"]] = (target["file"], extracted["start_line"])
                if line_count == 0 or line_count > target["max_proof_lines"]:
                    errors.append(f"proof length violation: {target['name']}")
                if not all(required.values()):
                    errors.append(f"required premise missing: {target['name']}")
                if any(forbidden.values()):
                    errors.append(f"forbidden target tactic present: {target['name']}")
                if not all(required_tokens.values()):
                    errors.append(f"required proof method token missing: {target['name']}")
                if target.get("require_induction") and not contains_token(clean_body, "induction"):
                    errors.append(f"required induction missing: {target['name']}")
                if re.search(
                    r"(?m)^\s*(?:(?:theorem|lemma|def|class|structure|inductive|instance|namespace|section|end|macro|macro_rules|syntax|elab|attribute|initialize|builtin_initialize)\b|#)",
                    clean_body,
                ):
                    errors.append(f"declaration injection inside target proof: {target['name']}")
            except Exception as exc:
                target_results[target["name"]] = {"error": str(exc)}
                errors.append(f"cannot inspect target proof {target['name']}: {exc}")
        result["target_checks"] = target_results

        for target in policy["targets"] if policy["mode"] == "proved" else []:
            for prerequisite in target.get("depends_on", []):
                required_position = positions.get(prerequisite)
                current_position = positions.get(target["name"])
                if required_position is None or current_position is None:
                    errors.append(f"dependency position unavailable: {prerequisite} -> {target['name']}")
                elif required_position[0] == current_position[0] and required_position[1] >= current_position[1]:
                    errors.append(f"dependency declaration order violated: {prerequisite} -> {target['name']}")

        forbidden_files: dict[str, list[str]] = {}
        for path in sorted(repo.rglob("*.lean")):
            clean = strip_lean_comments(path.read_text(encoding="utf-8"))
            found = [token for token in GLOBAL_FORBIDDEN if contains_token(clean, token)]
            if found:
                forbidden_files[str(path.relative_to(repo))] = found
        result["global_forbidden_scan"] = forbidden_files
        if forbidden_files:
            errors.append("globally forbidden Lean construct present")
        if policy['mode'] == 'unprovable':
            counter_path = repo / policy['counterexample']['file']
            counter_source = strip_lean_comments(counter_path.read_text(encoding='utf-8'))
            # Unlike the fixed project, this entire file is submitted code.
            # Reject executable elaboration commands before the first compile.
            executable_commands = re.findall(
                r'(?m)^\s*(?:#\w+|(?:unsafe\s+)?(?:initialize|builtin_initialize|macro|macro_rules|syntax|elab|attribute|export|set_option|run_cmd|run_tac)\b)',
                counter_source,
            )
            if executable_commands or re.search(r'@\[\s*(?:extern|implemented_by)', counter_source):
                errors.append('counterexample contains executable elaboration or trust-changing commands')
            imports = re.findall(r'(?m)^\s*import\s+([^\s]+)\s*$', counter_source)
            if imports != ['Main']:
                errors.append('counterexample must import exactly Main')
        if errors:
            write_result(result_path, result)
            return 2

        lake_build = run_command(["lake", "build"], repo, home, 600)
        result["lake_build"] = lake_build
        if lake_build["exit_code"] != 0:
            errors.append("clean lake build failed")
        if not validation_has(proof, ["lake", "build"]):
            quality.append("proof report lacks successful lake build evidence")

        if policy["mode"] == "proved" and lake_build["exit_code"] == 0:
            semantic_checks = [
                (target["name"], required)
                for target in policy["targets"]
                for required in target.get("required_refs", [])
            ]
            semantic_dependencies = run_dependency_checker(repo, home, "Main", semantic_checks)
            result["semantic_dependencies"] = semantic_dependencies
            if semantic_dependencies is not None and semantic_dependencies["exit_code"] != 0:
                errors.append("a required theorem is only mentioned textually, not retained by the elaborated proof")

        if policy["mode"] == "proved":
            if proof.get("counterexample") is not None:
                quality.append("proved case must report a null counterexample")
        else:
            counterexample = proof.get("counterexample")
            expected = policy["counterexample"]
            if not isinstance(counterexample, dict) or set(counterexample) != {"target", "witness", "file", "declaration"}:
                errors.append("counterexample report has the wrong shape")
            else:
                if counterexample.get("target") != expected["target"]:
                    errors.append("counterexample target mismatch")
                if counterexample.get("witness") != expected["witness"]:
                    errors.append("counterexample witness mismatch")
                if counterexample.get("file") != expected["file"]:
                    errors.append("counterexample file mismatch")
                declaration = counterexample.get("declaration")
                if not isinstance(declaration, str) or not SAFE_DECL.fullmatch(declaration):
                    errors.append("counterexample declaration is not a safe fully qualified name")
                else:
                    counter_path = repo / expected["file"]
                    source = counter_path.read_text(encoding="utf-8") if counter_path.is_file() else ""
                    clean_source = strip_lean_comments(source)
                    imports = re.findall(r"(?m)^\s*import\s+([^\s]+)\s*$", clean_source)
                    if imports != ["Main"]:
                        errors.append("counterexample must import exactly Main")
                    if expected["target"] not in clean_source:
                        errors.append("counterexample source does not state the exact target negation")
                    leaf = declaration.rsplit(".", 1)[-1]
                    try:
                        extracted = declaration_proof(source, leaf)
                        clean_body = strip_lean_comments(extracted["body"])
                        line_count = proof_noncomment_lines(extracted["body"])
                        result["counterexample_source"] = {
                            "proof_lines": line_count,
                            "max_proof_lines": expected["max_proof_lines"],
                            "required_refs": {
                                ref: ref in clean_body for ref in expected["required_refs"]
                            },
                        }
                        if line_count == 0 or line_count > expected["max_proof_lines"]:
                            errors.append("counterexample proof length violation")
                        if not all(ref in clean_body for ref in expected["required_refs"]):
                            errors.append("counterexample proof misses a required target premise")
                        if expected["witness"] not in clean_body:
                            errors.append("counterexample proof does not name the required concrete witness")
                    except Exception as exc:
                        errors.append(f"cannot inspect counterexample proof: {exc}")
                    counter_compile = run_command(["lake", "env", "lean", expected["file"]], repo, home, 600)
                    result["counterexample_compile"] = counter_compile
                    if counter_compile["exit_code"] != 0:
                        errors.append("counterexample file failed to compile")
                    module_compile = run_command(
                        [
                            "lake", "env", "lean",
                            "-o", ".lake/build/lib/lean/Counterexample.olean",
                            expected["file"],
                        ],
                        repo,
                        home,
                        600,
                    )
                    result["counterexample_module_compile"] = module_compile
                    if module_compile["exit_code"] != 0:
                        errors.append("counterexample module artifact failed to compile")
                    else:
                        counterexample_dependencies = run_dependency_checker(
                            repo,
                            home,
                            "Counterexample",
                            [(declaration, required) for required in expected["required_refs"]],
                        )
                        result["counterexample_semantic_dependencies"] = counterexample_dependencies
                        if (
                            counterexample_dependencies is not None
                            and counterexample_dependencies["exit_code"] != 0
                        ):
                            errors.append(
                                "a counterexample premise is only mentioned textually, not retained by the elaborated proof"
                            )
                    checker = repo / "FormalEvaluatorLink.lean"
                    checker.write_text(
                        f"import Counterexample\n\nexample : ¬ {expected['target']} := {declaration}\n",
                        encoding="utf-8",
                    )
                    semantic = run_command(["lake", "env", "lean", checker.name], repo, home, 600)
                    result["semantic_counterexample_link"] = semantic
                    checker.unlink(missing_ok=True)
                    if semantic["exit_code"] != 0:
                        errors.append("reported declaration is not a proof of the exact target negation")
            if not validation_has(proof, ["lake", "env", "lean", "Counterexample.lean"]):
                quality.append("proof report lacks successful counterexample compilation evidence")

    result["validity_gate"] = not errors
    write_result(result_path, result)
    return 0 if result["validity_gate"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    return evaluate(args.case_dir.resolve(), args.output_dir.resolve(), args.result.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
