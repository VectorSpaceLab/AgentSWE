"""Durable evaluator-owned reservations for Aider public product executions."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def create(path: Path, value):
    """Publish once and persist the file plus its directory before dispatch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def checkpoint(path: Path, value):
    """Replace evaluator state atomically; immutable attempts use create()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pending")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


class ProductAttempts:
    def __init__(self, run: Path, context: dict):
        self.root = run / "product_attempts"
        self.cross_run_guard = Path(os.environ["AGENTSWE_AIDER_PRIOR_PRODUCT_GUARD"]).resolve() if os.environ.get("AGENTSWE_AIDER_PRIOR_PRODUCT_GUARD") else None
        self.cross_run_guard_sha256 = hashlib.sha256(self.cross_run_guard.read_bytes()).hexdigest() if self.cross_run_guard and self.cross_run_guard.is_file() else None
        self.cross_run_guard_binding = context.get("cross_run_product_guard")
        if self.cross_run_guard is not None:
            if not isinstance(self.cross_run_guard_binding, dict) or self.cross_run_guard_binding.get("path") != str(self.cross_run_guard) or self.cross_run_guard_binding.get("sha256") != self.cross_run_guard_sha256:
                raise RuntimeError("cross-run product guard is not hash-bound to product context")
        marker = self.root / "context.json"
        self.legacy = any(run.iterdir()) and not marker.is_file()
        if self.legacy:
            return  # Existing unbound runs are read-only, never silently adopted.
        if marker.exists():
            if read(marker) != context:
                raise RuntimeError("public attempt context changed; preserve original run")
        else:
            create(marker, context)

    @contextmanager
    def locked(self):
        if self.legacy:
            yield
            return
        with (self.root / "controller.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle, fcntl.LOCK_UN)

    def directory(self, delivery: str):
        return self.root / "deliveries" / delivery

    def previous(self, delivery: str):
        path = self.directory(delivery)
        if not (path / "intent.json").exists():
            return None
        alias = path / "product_alias.json"
        if alias.exists():
            return self.previous(read(alias)["original_delivery_digest"])
        if (path / "result.json").is_file():
            result = read(path / "result.json")
            for item, expected in read(path / "evidence.json").items():
                p = Path(item)
                if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                    raise RuntimeError("preserved public attempt evidence changed")
            return {**result, "duplicate": True, "round_consumed": False}
        return {"accepted": False, "round_consumed": False, "duplicate": True,
                "classification": "evaluator_infrastructure_failure",
                "state": "unresolved_previous_attempt", "candidate_digest": delivery,
                "error": "Previous product execution has no complete checkpoint; automatic replay is blocked",
                "attempt_path": str(path)}

    def reserve_delivery(self, delivery: str, metadata: dict):
        if self.legacy:
            raise RuntimeError("legacy run lacks durable product intents; no new execution permitted")
        path = self.directory(delivery)
        create(path / "intent.json", metadata)
        return path

    def _cross_run_product_rejection(self, product: str, delivery: str):
        """Reject a product already observed in the campaign's immutable prior runs.

        The guard file is evaluator-owned and hash-bound into the new run context;
        it contains only stable product digests and immutable evidence references.
        No previous request or product is replayed or read by the Builder.
        """
        if self.cross_run_guard is None:
            return None
        try:
            if self.cross_run_guard_sha256 is None or hashlib.sha256(self.cross_run_guard.read_bytes()).hexdigest() != self.cross_run_guard_sha256:
                raise RuntimeError("cross-run product guard changed after context binding")
            guard = read(self.cross_run_guard)
            if guard.get("schema_version") != "agentswe-aider-cross-run-product-guard/v1":
                raise RuntimeError("invalid cross-run product guard schema")
            entries = guard.get("prior_products")
            if not isinstance(entries, list):
                raise RuntimeError("invalid cross-run product guard entries")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("stable_product_digest"), str):
                    raise RuntimeError("invalid cross-run product guard entry")
                evidence_path = entry.get("evidence_path")
                evidence_sha = entry.get("evidence_sha256")
                if not isinstance(evidence_path, str) or not isinstance(evidence_sha, str):
                    raise RuntimeError("cross-run product guard evidence binding missing")
                evidence = Path(evidence_path)
                if not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_sha:
                    raise RuntimeError("cross-run product guard evidence drift")
                identity = read(evidence)
                if identity.get("stable_product_digest") != entry["stable_product_digest"] or identity.get("delivery_digest") != entry.get("delivery_digest"):
                    raise RuntimeError("cross-run product guard evidence identity mismatch")
                for unknown in entry.get("unknown_request_refs", []):
                    if not isinstance(unknown, dict) or not isinstance(unknown.get("path"), str) or not isinstance(unknown.get("sha256"), str):
                        raise RuntimeError("cross-run product guard unknown reference malformed")
                    up = Path(unknown["path"])
                    if not up.is_file() or hashlib.sha256(up.read_bytes()).hexdigest() != unknown["sha256"]:
                        raise RuntimeError("cross-run product guard unknown evidence drift")
                if entry["stable_product_digest"] == product:
                    return {"accepted": False, "round_consumed": False,
                            "classification": "evaluator_infrastructure_failure",
                            "state": "cross_run_prior_product_rejected",
                            "candidate_digest": delivery, "stable_product_digest": product,
                            "error": "stable product digest is present in immutable prior campaign runs; downstream dispatch blocked",
                            "cross_run_guard": str(self.cross_run_guard)}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("cross-run product guard unreadable") from exc
        return None

    def reserve_product(self, product: str, delivery: str):
        rejection = self._cross_run_product_rejection(product, delivery)
        if rejection is not None:
            return rejection
        path = self.root / "products" / (product + ".json")
        if path.exists():
            original = read(path)["delivery_digest"]
            create(self.directory(delivery) / "product_alias.json", {"original_delivery_digest": original})
            result = self.previous(original)
            if result is None:
                raise RuntimeError("product intent has no bound delivery")
            return {**result, "same_product": True, "stable_product_digest": product}
        create(path, {"delivery_digest": delivery, "stable_product_digest": product})
        return None

    def finish(self, delivery: str, record: dict):
        path = self.directory(delivery)
        evidence = {}
        for child in path.rglob("*"):
            if child.relative_to(path).parts[0] not in {"evaluations", "checkpoints", "intents"}:
                continue
            if child.is_file() and not child.is_symlink():
                evidence[str(child)] = hashlib.sha256(child.read_bytes()).hexdigest()
        create(path / "evidence.json", evidence)
        create(path / "result.json", record)
