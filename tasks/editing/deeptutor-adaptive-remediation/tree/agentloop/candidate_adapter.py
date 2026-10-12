#!/usr/bin/env python3
"""Materialize and compile-check one DeepTutor Edit Candidate."""

from __future__ import annotations

import argparse
import compileall
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from .runtime_probe import probe_runtime
except ImportError:  # pragma: no cover
    from runtime_probe import probe_runtime  # type: ignore

try:
    from .protocol import (
        AUTHORITATIVE_SOURCE,
        AUTHORITATIVE_SOURCE_DIGEST,
        changed_paths,
        file_digest,
        tree_digest,
        utc_now,
        validate_delivery,
        write_json,
    )
except ImportError:
    from protocol import (  # type: ignore
        AUTHORITATIVE_SOURCE,
        AUTHORITATIVE_SOURCE_DIGEST,
        changed_paths,
        file_digest,
        tree_digest,
        utc_now,
        validate_delivery,
        write_json,
    )


COMPILE_TARGETS = (
    "deeptutor/capabilities/mastery",
    "deeptutor/learning",
    "deeptutor/api/routers/mastery_path.py",
    "deeptutor/services/path_service.py",
)
REQUIRED_PRODUCT_ENTRIES = (
    "deeptutor/capabilities/mastery/capability.py",
    "deeptutor/agents/chat/agentic_pipeline.py",
    "deeptutor/agents/chat/agent_loop.py",
    "deeptutor/capabilities/mastery/tools.py",
)


def _run(command: list[str], *, cwd: Path, timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def materialize_candidate(
    source_repository: Path,
    candidate_delivery: Path,
    build_dir: Path,
    *,
    compile_candidate: bool = True,
    runtime_python: str | None = None,
    readiness: bool = False,
) -> dict[str, Any]:
    source_repository = source_repository.resolve()
    candidate_delivery = candidate_delivery.resolve()
    build_dir = build_dir.resolve()
    errors = validate_delivery(candidate_delivery, readiness=readiness)
    manifest: dict[str, Any] = {
        "schema_version": "agentswe-deeptutor-candidate-build/v1",
        "created_at": utc_now(),
        "source_repository": str(source_repository),
        "source_digest": tree_digest(source_repository) if source_repository.is_dir() else None,
        "expected_source_digest": AUTHORITATIVE_SOURCE_DIGEST,
        "candidate_delivery": str(candidate_delivery),
        "candidate_digest": tree_digest(candidate_delivery) if candidate_delivery.is_dir() else None,
        "classification": "candidate_delivery_failure" if errors else "candidate_ready",
        "valid": False,
        "errors": errors,
    }
    if errors:
        write_json(build_dir / "build_manifest.json", manifest)
        return manifest
    if source_repository == AUTHORITATIVE_SOURCE.resolve() and manifest["source_digest"] != AUTHORITATIVE_SOURCE_DIGEST:
        manifest.update(
            classification="evaluator_infrastructure_error",
            infra_valid=False,
            errors=["authoritative product source digest mismatch"],
        )
        write_json(build_dir / "build_manifest.json", manifest)
        return manifest

    repository = build_dir / "repository"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    shutil.copytree(source_repository, repository, symlinks=True)
    patch = candidate_delivery / "solution.patch"
    manifest["patch_sha256"] = file_digest(patch)
    manifest["changed_paths"] = changed_paths(patch)
    checks: list[dict[str, Any]] = []
    checks.append(_run(["git", "init", "-q"], cwd=repository, timeout=30))
    checks.append(_run(["git", "apply", "--check", str(patch)], cwd=repository))
    if checks[-1]["exit_code"] == 0:
        checks.append(_run(["git", "apply", str(patch)], cwd=repository))
    manifest["patch_checks"] = checks
    if any(item["exit_code"] != 0 for item in checks):
        manifest.update(
            classification="candidate_patch_failure",
            errors=["git apply check/apply failed"],
        )
        write_json(build_dir / "build_manifest.json", manifest)
        return manifest

    missing_entries = [entry for entry in REQUIRED_PRODUCT_ENTRIES if not (repository / entry).is_file()]
    manifest["missing_product_entries"] = missing_entries
    if missing_entries:
        manifest.update(
            classification="candidate_build_failure",
            errors=["required DeepTutor product entries missing"],
        )
        write_json(build_dir / "build_manifest.json", manifest)
        return manifest

    compile_ok: bool | None = None
    product_before = tree_digest(repository)
    if compile_candidate and readiness:
        if not runtime_python:
            raise ValueError('readiness requires evaluator runtime Python')
        from .isolated_runtime import sandbox_command
        from .owned_resources import run_owned
        runtime_root=build_dir/'compile_runtime';runtime_root.mkdir()
        script="import pathlib,sys; root=pathlib.Path(sys.argv[1]); [compile(p.read_bytes(),str(p),'exec') for p in root.rglob('*.py') if '.git' not in p.relative_to(root).parts]"
        command,boundary=sandbox_command([runtime_python,'-I','-B','-c',script,str(repository)],repository,runtime_root,runtime_python,repository_readonly=True)
        completed,resource=run_owned(command,cwd='/',env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'},output=build_dir/'compile_resources',timeout=120)
        compile_ok=completed.returncode==0 and resource.get('valid') is True and resource.get('cleanup',{}).get('complete') is True
        manifest['readiness_compile']={'exit_code':completed.returncode,'resources':resource,'sandbox':boundary,'python':runtime_python,'python_sha256':file_digest(Path(runtime_python))}
    elif compile_candidate:
        compile_ok = all(
            compileall.compile_dir(str(repository / target), quiet=1, force=False)
            if (repository / target).is_dir()
            else compileall.compile_file(str(repository / target), quiet=1, force=False)
            for target in COMPILE_TARGETS
        )
    manifest["python_compile_checked"] = compile_candidate
    manifest["python_compile_ok"] = compile_ok
    if compile_candidate and not compile_ok:
        manifest.update(
            classification="candidate_build_failure",
            errors=["targeted Python compile failed"],
        )
        write_json(build_dir / "build_manifest.json", manifest)
        return manifest

    runtime_record: dict[str, Any] | None = None
    if runtime_python:
        runtime_record = probe_runtime(
            runtime_python,
            repository,
            output=build_dir / "runtime_probe.json",
        )
        manifest["runtime_probe"] = runtime_record
        if runtime_record.get("infra_valid") is not True:
            manifest.update(
                classification=runtime_record.get(
                    "classification", "runtime_dependency_infrastructure_error"
                ),
                infra_valid=False,
                errors=list(runtime_record.get("errors") or ["runtime probe failed"]),
            )
            write_json(build_dir / "build_manifest.json", manifest)
            return manifest

    manifest.update(
        valid=True,
        infra_valid=True,
        classification="candidate_ready",
        repository=str(repository),
        materialized_digest=tree_digest(repository),
        real_lower_entry=(
            "deeptutor.capabilities.mastery.capability.MasteryPathCapability.run"
        ),
        runtime_probe_checked=runtime_python is not None,
        runtime_ready=runtime_record is None or runtime_record.get("valid") is True,
    )
    if readiness:
        manifest['readiness_build']={'source_before':product_before,'source_after':tree_digest(repository),
            'build_exit_code':manifest['readiness_compile']['exit_code'],'toolchain_sha256':manifest['readiness_compile']['python_sha256']}
        if product_before!=tree_digest(repository) or manifest['source_digest']!=tree_digest(source_repository):
            manifest.update(valid=False,infra_valid=False,classification='evaluator_infrastructure_error',errors=['source changed during controlled build'])
    write_json(build_dir / "build_manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repository", type=Path, default=AUTHORITATIVE_SOURCE)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--skip-compile", action="store_true")
    parser.add_argument("--runtime-python")
    args = parser.parse_args()
    result = materialize_candidate(
        args.source_repository,
        args.candidate,
        args.build_dir,
        compile_candidate=not args.skip_compile,
        runtime_python=args.runtime_python,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
