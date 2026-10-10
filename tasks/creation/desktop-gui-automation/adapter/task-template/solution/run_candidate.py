#!/usr/bin/env python3
"""Orchestrate one GUI Candidate through physically isolated Compose services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


def digest(root: Path) -> str:
    value = hashlib.sha256()
    root = root.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, data = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, data = b"F", path.read_bytes()
        elif path.is_dir():
            continue
        else:
            kind, data = b"O", b""
        value.update(kind + len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data)
    return value.hexdigest()


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def wait_json(path: Path, timeout: float, error_paths: tuple[Path, ...] = ()) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "file not present"
    while time.monotonic() < deadline:
        for error in error_paths:
            if error.is_file():
                raise RuntimeError(error.read_text(encoding="utf-8", errors="replace")[:4000])
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
                last_error = "JSON value is not an object"
            except (OSError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {path}: {last_error}")


def publish_tree(source: Path, target: Path) -> dict[str, Any]:
    """Copy a tree (regular files, symlinks as links, directories); special files are skipped and reported."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    files = links = dirs = 0
    skipped: list[str] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            os.symlink(os.readlink(path), destination)
            links += 1
        elif path.is_dir():
            destination.mkdir(exist_ok=True)
            dirs += 1
        elif path.is_file():
            shutil.copyfile(path, destination)
            files += 1
        else:
            skipped.append(relative.as_posix())
    return {"source": str(source), "target": str(target), "files": files, "symlinks": links,
            "directories": dirs, "skipped_special": skipped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--submission", required=True, type=Path)
    parser.add_argument("--staged-input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--stderr", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--publish", type=Path, default=None,
                        help="v2-lite: directory (Harbor host bind) that receives copies of output and evidence")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    before = digest(args.submission)
    if before != manifest["candidate_digest"]:
        raise RuntimeError("staged Candidate digest mismatch")
    staged_bytes = args.staged_input.read_bytes()
    if hashlib.sha256(staged_bytes).hexdigest() != manifest["staged_input_sha256"]:
        raise RuntimeError("staged active input digest mismatch")
    args.output.mkdir(parents=True, exist_ok=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    args.control.mkdir(parents=True, exist_ok=True)
    # The three directories are live named volumes owned by concurrently
    # starting services.  In particular, gui-control creates its browser
    # evidence before this orchestrator observes ready.json.  Moving non-empty
    # directories here races that startup and can remove the screenshots
    # directory while Playwright is writing the initial capture.  Harbor gives
    # each task a fresh Compose project/volume set; retry archiving happens at
    # the outer task/result boundary, not inside these live volumes.

    request_id = uuid.uuid4().hex
    request = {
        "schema_version": "1.0", "request_id": request_id,
        "case_id": manifest["case_id"], "case_digest": manifest["case_digest"],
        "candidate_digest": before, "source_input_sha256": manifest["source_input_sha256"],
        "staged_input_sha256": manifest["staged_input_sha256"],
        "application_links_rewritten": manifest["application_links_rewritten"],
        "timeout_seconds": args.timeout, "memory_limit_mib": 4096,
    }
    wait_json(args.evidence / "ready.json", 90, (args.evidence / "infrastructure_error.json",))
    wait_json(args.control / "runner-ready.json", 30, (args.control / "runner-error.json",))
    atomic_json(args.control / "request.json", request)
    runner = wait_json(args.control / "result.json", args.timeout + 60, (args.control / "runner-error.json",))
    if runner.get("request_id") != request_id or runner.get("case_id") != manifest["case_id"]:
        raise RuntimeError("Candidate runner returned mismatched identity")
    args.stdout.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.control / "candidate.stdout", args.stdout)
    shutil.copyfile(args.control / "candidate.stderr", args.stderr)
    atomic_json(args.evidence / "finalize.request.json", {"request_id": request_id})
    wait_json(args.evidence / "finalized.json", 90, (args.evidence / "infrastructure_error.json",))

    execution = wait_json(args.evidence / "execution.json", 5)
    after = digest(args.submission)
    output_dir, evidence_dir = args.output, args.evidence
    publish_report = None
    if args.publish is not None:
        # v2-lite (0917): the shared volumes are mounted outside Harbor's /logs/artifacts bind, so copy
        # them into it here (evaluator-owned orchestrator; the Candidate cannot write to this service).
        output_dir = args.publish / "candidate_output"
        evidence_dir = args.publish / "trusted_evidence"
        publish_report = {
            "candidate_output": publish_tree(args.output, output_dir),
            "trusted_evidence": publish_tree(args.evidence, evidence_dir),
        }
    evidence = {
        "schema_version": "2.0", "case_id": manifest["case_id"],
        "candidate_digest_before": before, "candidate_digest_after": after,
        "case_digest_before": manifest["case_digest"], "case_digest_after": manifest["case_digest"],
        "candidate_unchanged": before == after, "case_unchanged": True,
        "candidate_exit_code": execution.get("exit_code"), "harness_exit_code": 0,
        "timed_out": execution.get("timeout", False), "harness_timed_out": False,
        "output_digest": digest(output_dir), "output_structure_valid": True,
        "process_log_paths_valid": args.stdout.is_file() and args.stderr.is_file(),
        "evidence_path_valid": True, "harness_evidence": str(evidence_dir),
        "isolation_protocol": "compose-isolated-gui-v1",
    }
    if publish_report is not None:
        evidence["published_from"] = {"candidate_output": str(args.output), "trusted_evidence": str(args.evidence)}
        evidence["publish_report"] = publish_report
    if execution.get("infrastructure_error"):
        evidence["infrastructure_error"] = execution["infrastructure_error"]
    atomic_json((args.publish if args.publish is not None else args.evidence.parent) / "run_evidence.json", evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
