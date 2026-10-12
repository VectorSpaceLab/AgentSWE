"""Evaluator-owned pre-submit validation; no provider calls or Candidate admission."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

REQUIRED = ("solution.patch", "edit_report.json", "run_report.json")
META = ("builder_session_id", "submission_number", "revision_of_candidate_digest", "feedback_digest")
RUN_FIELDS = {"schema_version", "status", "artifact_paths", "errors", "runtime_seconds", "peak_memory_bytes", "api_calls"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    """Product digest including entry types and modes, excluding only Git metadata."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            payload = os.readlink(path).encode()
        elif stat.S_ISREG(info.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISDIR(info.st_mode):
            payload = b""
        else:
            raise ValueError("special file in source")
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(str(stat.S_IFMT(info.st_mode) | (info.st_mode & 0o111)).encode() + b"\0")
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def _root(raw):
    if ".." in Path(raw).parts:
        raise ValueError("directory lexical traversal")
    path = Path(os.path.abspath(raw))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("directory has symlink component")
    if not path.is_dir():
        raise ValueError("directory missing")
    return path


def safe_path(raw, *, prefixed=False, allow_null=False):
    if allow_null and raw == "/dev/null":
        return None
    if not isinstance(raw, str) or not raw or "\\" in raw or any(ord(c) < 32 for c in raw):
        raise ValueError("unsafe patch path encoding")
    if prefixed:
        if not raw.startswith(("a/", "b/")):
            raise ValueError("patch path lacks repository prefix")
        raw = raw[2:]
    parts = raw.split("/")
    if PurePosixPath(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw) or any(p in ("", ".", "..") for p in parts):
        raise ValueError("unsafe patch path: " + raw)
    forbidden = {".git", ".ssh", ".aws", ".azure", ".config", ".env", "credentials", "secrets", "tmp", "temp"}
    if any(p.lower() in forbidden or p.lower().startswith(".env.") or p.lower().endswith((".pem", ".key", ".swp", ".tmp")) for p in parts):
        raise ValueError("forbidden patch path: " + raw)
    return raw


def paths(path):
    text = Path(path).read_text()
    found = set()
    headers = 0
    in_hunk = False
    old_remaining = new_remaining = 0
    for line in text.splitlines():
        if in_hunk:
            if line.startswith("\\ No newline at end of file"):
                continue
            if line.startswith(" "):
                old_remaining -= 1; new_remaining -= 1
            elif line.startswith("-"):
                old_remaining -= 1
            elif line.startswith("+"):
                new_remaining -= 1
            else:
                raise ValueError("malformed patch hunk")
            if min(old_remaining, new_remaining) < 0:
                raise ValueError("patch hunk count mismatch")
            in_hunk = bool(old_remaining or new_remaining)
            continue
        if line.startswith("diff --git "):
            in_hunk = False
            values = shlex.split(line[len("diff --git "):])
            if len(values) != 2:
                raise ValueError("malformed diff header")
            found.update(safe_path(value, prefixed=True) for value in values)
            headers += 1
        elif line.startswith(("diff --cc ", "diff --combined ")):
            raise ValueError("combined patches unsupported")
        elif line.startswith("@@"):
            match = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            if not match:
                raise ValueError("malformed hunk header")
            old_remaining, new_remaining = (int(v) if v is not None else 1 for v in match.groups())
            in_hunk = bool(old_remaining or new_remaining)
        elif not in_hunk and line.startswith(("--- ", "+++ ")):
            raw = line[4:].split("\t", 1)[0]
            values = shlex.split(raw) if raw.startswith('"') else [raw]
            if len(values) != 1:
                raise ValueError("malformed file header")
            value = safe_path(values[0], prefixed=True, allow_null=True)
            if value:
                found.add(value)
        elif not in_hunk and line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            prefix = next(p for p in ("rename from ", "rename to ", "copy from ", "copy to ") if line.startswith(p))
            raw = line[len(prefix):]
            values = shlex.split(raw) if raw.startswith('"') else [raw]
            if len(values) != 1:
                raise ValueError("malformed rename/copy header")
            found.add(safe_path(values[0]))
        elif not in_hunk and (line.startswith(("old mode ", "new mode ", "new file mode ", "deleted file mode ")) or line.startswith("index ")):
            mode = line.split()[-1]
            if re.fullmatch(r"\d{6}", mode) and mode not in ("100644", "100755"):
                raise ValueError("symlink/submodule/special mode unsupported")
        elif line.startswith(("GIT binary patch", "Binary files ")):
            raise ValueError("binary patches unsupported")
    if not headers or not found:
        raise ValueError("patch has no Git file headers")
    if in_hunk:
        raise ValueError("incomplete patch hunk")
    return sorted(found)


def report_ok(candidate, patch_paths, expected):
    edit = json.loads((candidate / "edit_report.json").read_text())
    run = json.loads((candidate / "run_report.json").read_text())
    if not isinstance(edit, dict) or not isinstance(run, dict):
        raise ValueError("reports must be JSON objects")
    if edit.get("schema_version") != "1.0" or not isinstance(edit.get("feature_summary"), str) or not edit["feature_summary"].strip():
        raise ValueError("edit_report schema_version/feature_summary invalid")
    if not isinstance(edit.get("changed_paths"), list) or sorted(edit["changed_paths"]) != patch_paths:
        raise ValueError("edit_report changed_paths must equal patch paths")
    commands = edit.get("commands")
    if not isinstance(commands, list) or any(not isinstance(v, dict) or not {"command", "exit_code", "result"} <= set(v)
            or not isinstance(v["command"], str) or type(v["exit_code"]) is not int or not isinstance(v["result"], str) for v in commands):
        raise ValueError("edit_report commands invalid")
    for field in ("compatibility_notes", "limitations"):
        if not isinstance(edit.get(field), list) or any(not isinstance(v, str) for v in edit[field]):
            raise ValueError("edit_report " + field + " must be string array")
    if set(run) != RUN_FIELDS | set(META):
        raise ValueError("run_report must contain shared fields and lifecycle metadata exactly")
    if run.get("schema_version") != "1.0" or not isinstance(run.get("status"), str) or not run["status"].strip():
        raise ValueError("run_report version/status invalid")
    if not isinstance(run.get("artifact_paths"), list) or sorted(run["artifact_paths"]) != sorted(REQUIRED):
        raise ValueError("run_report artifact_paths invalid")
    if not isinstance(run.get("errors"), list) or any(not isinstance(v, str) for v in run["errors"]):
        raise ValueError("run_report errors invalid")
    value = run.get("runtime_seconds")
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("run_report runtime_seconds invalid")
    if type(run.get("peak_memory_bytes")) is not int or run["peak_memory_bytes"] < 0:
        raise ValueError("run_report peak_memory_bytes invalid")
    usage = run.get("api_calls")
    if not isinstance(usage, dict) or set(usage) != {"gateway", "serper", "web_retrieval"} or any(type(v) is not int or v < 0 for v in usage.values()):
        raise ValueError("run_report api_calls invalid")
    if any(run[k] != expected[k] for k in META):
        raise ValueError("run_report differs from evaluator expected metadata")
    if expected["submission_number"] == 2 and (not isinstance(edit.get("feedback_response"), str) or not edit["feedback_response"].strip()):
        raise ValueError("revision must explain feedback response")


def validate(candidate, source_repo, *, expected_metadata=None, metadata=None, build_runner=None,
             expected_toolchain_digest=None, product_digest=tree, delivery_validator=None,
             previous_materialized_digest=None):
    """Callbacks/expected values must originate from evaluator, never submission.

    product_digest may use the task's trusted product-surface algorithm. It is
    called by this validator both before and after the controlled build.
    """
    checks = {}
    def result(errors):
        return dict(valid=not errors, classification="candidate_ready" if not errors else "candidate_delivery_failure", errors=errors, checks=checks)
    try:
        c, source = _root(candidate), _root(source_repo)
        if set(p.name for p in c.iterdir()) != set(REQUIRED) or any(not stat.S_ISREG((c / name).lstat().st_mode) for name in REQUIRED):
            raise ValueError("submission must contain exactly three regular non-symlink files")
        expected = expected_metadata
        if not isinstance(expected, dict) or set(expected) != set(META):
            raise ValueError("evaluator expected metadata required")
        if not isinstance(expected["builder_session_id"], str) or not expected["builder_session_id"] or type(expected["submission_number"]) is not int or expected["submission_number"] not in (1, 2):
            raise ValueError("evaluator session/round invalid")
        for field in META[2:]:
            value = expected[field]
            if (expected["submission_number"] == 1 and value is not None) or (expected["submission_number"] == 2 and (not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value))):
                raise ValueError("evaluator revision/feedback metadata invalid")
        if metadata is not None and metadata != expected:
            raise ValueError("supplied metadata differs from evaluator expectation")
        checks["patch_paths"] = paths(c / "solution.patch")
        report_ok(c, checks["patch_paths"], expected)
        if delivery_validator is not None:
            delivery_validator(c)
        if build_runner is None or not isinstance(expected_toolchain_digest, str) or not re.fullmatch("[0-9a-f]{64}", expected_toolchain_digest):
            raise ValueError("controlled evaluator build and toolchain binding required")
        for path in source.rglob("*"):
            if path.is_symlink() and ".git" not in path.relative_to(source).parts:
                target = (path.parent / os.readlink(path)).resolve()
                if target != source and source not in target.parents:
                    raise ValueError("pristine repository symlink escape")
        source_before = product_digest(source)
        checks["submission_sha256"] = {name: sha(c / name) for name in REQUIRED}
        with tempfile.TemporaryDirectory(prefix="presubmit-") as temp:
            repo = Path(temp) / "repo"
            shutil.copytree(source, repo, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            for name in checks["patch_paths"]:
                path = repo
                for part in PurePosixPath(name).parts:
                    path = path / part
                    if path.is_symlink():
                        raise ValueError("patch traverses pristine symlink")
            env = dict(PATH="/usr/bin:/bin", LANG="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", HOME=temp)
            def git(*args):
                return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=120)
            if git("init", "-q").returncode:
                raise ValueError("cannot initialize disposable repository")
            check = git("apply", "--check", str(c / "solution.patch"))
            checks["patch_check_exit"] = check.returncode
            if check.returncode:
                raise ValueError("patch does not apply to pristine repository")
            applied = git("apply", str(c / "solution.patch"))
            checks["patch_apply_exit"] = applied.returncode
            if applied.returncode:
                raise ValueError("patch application failed")
            before = product_digest(repo)
            checks["materialized_source_digest_before_build"] = before
            if previous_materialized_digest is not None and before == previous_materialized_digest:
                raise ValueError("revision has no product change")
            build = build_runner(repo)
            after = product_digest(repo)
            checks["materialized_source_digest_after_build"] = after
            checks["build"] = build
            if before != after:
                raise ValueError("controlled build mutated materialized source")
            if not isinstance(build, dict) or type(build.get("exit_code")) is not int or build["exit_code"] != 0 or build.get("toolchain_digest") != expected_toolchain_digest:
                raise ValueError("controlled build failed/toolchain mismatch")
            if product_digest(source) != source_before:
                raise ValueError("pristine source drift")
            if {name: sha(c / name) for name in REQUIRED} != checks["submission_sha256"]:
                raise ValueError("submission changed during validation")
        return result([])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return result([str(exc)])
