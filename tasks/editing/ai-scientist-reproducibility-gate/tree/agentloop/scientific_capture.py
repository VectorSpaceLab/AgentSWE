"""Evaluator-owned immutable byte snapshots, never model-authored artifacts."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SCIENCE = ("claim_ledger.json", "verification_report.json", "validated_writeup.md")
CAPTURE = (*SCIENCE, "reproducibility_capsule.zip", "transaction_receipt.json",
           "attestation.json", "notification_receipt.json")


def public_checks():
    name = "agentswe_ai_scientific_public_checks"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "dev_cases/public_harness.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_regular(path: Path, root: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and root.resolve() in path.resolve().parents
    except OSError:
        return False


def capture_science(*, output: Path, workspace: Path, context: dict,
                    sequence: int, receipts: dict) -> dict:
    """Snapshot public product files after an action, before the next mutation.

    Missing or incorrect Candidate bindings are retained as evidence, not
    silently repaired. Only fixed scientific filenames and manifest-declared
    input references are copied. No private world, credentials or model output
    is mounted back into the product.
    """
    tx = receipts.get("transaction_receipt.json", {})
    tx = tx if isinstance(tx, dict) else {}
    source = output
    source_kind = "product_response"
    stage = tx.get("stage_path")
    if not all(safe_regular(output / name, output) for name in SCIENCE) and isinstance(stage, str):
        relative = Path(stage)
        sessions = Path(context.get("_session_store_path", output / "state/sessions"))
        possible = sessions / relative
        if not relative.is_absolute() and ".." not in relative.parts and sessions.resolve() in possible.resolve().parents:
            source, source_kind = possible, "receipt_referenced_stage"
    snapshot = output / "scientific_snapshots" / f"action_{sequence:03d}"
    if snapshot.exists():
        raise ValueError("refusing to overwrite an immutable scientific action snapshot")
    snapshot.mkdir(parents=True)
    files, unavailable = {}, []
    for name in CAPTURE:
        path = source / name if name not in {"transaction_receipt.json", "attestation.json", "notification_receipt.json"} else output / name
        root = source if path.parent == source else output
        if not safe_regular(path, root):
            continue
        if path.stat().st_size > 128 * 1024 * 1024:
            unavailable.append({"file": name, "reason": "snapshot exceeds explicit 128MiB evidence bound"})
            continue
        target = snapshot / "product" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(path, target)
        files[name] = sha(target)
    manifest = workspace / "evidence_manifest.json"
    input_files = {}
    if safe_regular(manifest, workspace):
        try:
            data = json.loads(manifest.read_text())
            references, _metrics = public_checks()._evidence_references(data, workspace)
            for name in {"evidence_manifest.json", *(ref["path"] for ref in references)}:
                path = workspace / name
                if not safe_regular(path, workspace):
                    continue
                target = snapshot / "workspace" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                input_files[name] = sha(target)
        except (ValueError, TypeError, KeyError) as exc:
            unavailable.append({"file": "evidence_manifest.json", "reason": type(exc).__name__})
    record = {"schema_version": "agentswe-ai-scientific-snapshot/v1", "sequence": sequence,
              "source_kind": source_kind, "receipt_stage_path": stage,
              "scientific_files_complete": all(name in files for name in SCIENCE),
              "files": files, "workspace_files": input_files, "unavailable": unavailable,
              "evaluator_owned_capture": True, "not_model_authored": True,
              "transaction_claimed_science_gate": tx.get("science_gate")}
    payload = snapshot / "capture.json"
    payload.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"path": payload.relative_to(output).as_posix(), "sha256": sha(payload)}
