#!/usr/bin/env python3
"""Provider-free OpenHands v2 binding and runner preflight.

This module acquires the shared coordinator's read lock through its canonical
``readiness_binding.verify_binding`` implementation. It never starts a broker,
loads a credential, or writes the shared registry/readiness gate.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROFILE = "single-dev-two-round-hidden-smoke-v1"
CONTROL_ROOT = Path("@@AGENTSWE_EDITING_CONTROL@@")
TASK_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def strict_json(path: Path) -> dict:
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError("duplicate binding JSON key")
            value[key] = child
        return value

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("readiness binding must be a JSON object")
    return value


def _shared_binding(control_root: Path):
    path = control_root / "readiness_binding.py"
    if path.is_symlink() or not path.is_file():
        raise ValueError("canonical readiness binding verifier is unavailable")
    spec = importlib.util.spec_from_file_location("openhands_shared_readiness_binding", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load canonical readiness binding verifier")
    if str(control_root) not in sys.path:
        sys.path.insert(0, str(control_root))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def load_and_verify_binding(
    binding_file: Path,
    expected_sha256: str,
    *,
    source_root: Path = TASK_ROOT,
    control_root: Path = CONTROL_ROOT,
) -> tuple[dict, dict]:
    binding_path = Path(binding_file)
    source = Path(source_root)
    control = Path(control_root)
    if not binding_path.is_absolute() or binding_path.is_symlink() or not binding_path.is_file():
        raise ValueError("readiness binding file must be an absolute regular file")
    if not is_hash(expected_sha256) or sha256(binding_path) != expected_sha256:
        raise ValueError("readiness binding file SHA mismatch")
    binding = strict_json(binding_path)
    shared, verifier_path = _shared_binding(control)
    measured = shared.verify_binding(source, binding, control_root=control)
    if measured != binding:
        raise ValueError("canonical verifier returned a different current binding")
    proof = {
        "binding_file": str(binding_path),
        "binding_sha256": expected_sha256,
        "source_root": str(source.resolve()),
        "control_root": str(control.resolve()),
        "binding_verifier": str(verifier_path),
        "binding_verifier_sha256": sha256(verifier_path),
        "measured_binding": measured,
        "registry_lock_mode": "shared",
    }
    return binding, proof


def provider_free_preflight(
    *,
    binding_file: Path,
    binding_sha256: str,
    source_root: Path,
    stage_root: Path,
    control_root: Path = CONTROL_ROOT,
) -> dict:
    binding, proof = load_and_verify_binding(
        binding_file, binding_sha256, source_root=source_root, control_root=control_root,
    )
    shared, _ = _shared_binding(Path(control_root))
    stage = Path(stage_root).resolve()
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError("OpenHands staging root must be a regular directory")
    return {
        "schema_version": "agentswe-openhands-v2-provider-free-preflight/v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "task": "openhands",
        "profile": PROFILE,
        "binding": binding,
        "binding_verification": proof,
        "stage_root": str(stage),
        "stage_tree_digest": shared.tree_digest(stage),
        "provider_calls": 0,
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "readiness_granted": False,
        "shared_registry_written": False,
        "shared_gate_written": False,
        "live_run_modified": False,
        "verification": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding-file", type=Path, required=True)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--stage-root", type=Path, default=TASK_ROOT)
    parser.add_argument("--control-root", type=Path, default=CONTROL_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = provider_free_preflight(
        binding_file=args.binding_file,
        binding_sha256=args.binding_sha256,
        source_root=args.source_root,
        stage_root=args.stage_root,
        control_root=args.control_root,
    )
    output = args.output.resolve()
    if output.exists():
        raise ValueError("preserve existing preflight receipt")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verification": "PASS", "receipt": str(output), "provider_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
