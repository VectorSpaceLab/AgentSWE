"""Evaluator-owned, receipt-bound exclusion of previously dispatched products."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import protocol

PATH_ENV = "AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD"
SHA_ENV = "AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD_SHA256"
PRODUCT_CACHE_EXCLUSIONS = {'__pycache__', '.pytest_cache'}


def product_digest(root: Path) -> str:
    """Product bytes independent of location and evaluator-generated metadata."""
    digest = hashlib.sha256()
    for item in sorted(root.rglob('*'), key=lambda path: path.relative_to(root).as_posix()):
        relative_path = item.relative_to(root)
        if relative_path.parts[0] == '.git' or any(part in PRODUCT_CACHE_EXCLUSIONS for part in relative_path.parts):
            continue
        relative = relative_path.as_posix().encode('utf-8')
        if item.is_symlink():
            payload = os.readlink(item).encode('utf-8')
            digest.update(b'L' + len(relative).to_bytes(8, 'big') + relative
                          + len(payload).to_bytes(8, 'big') + payload)
        elif item.is_file():
            digest.update(b'F' + len(relative).to_bytes(8, 'big') + relative)
            digest.update(item.stat().st_size.to_bytes(8, 'big'))
            with item.open('rb') as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _object(raw: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key in prior-product evidence")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("prior-product evidence must be a JSON object")
    return value


def _reference(ref: Any, *, parse: bool = True):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not _digest(ref.get("sha256")):
        raise ValueError("invalid prior-product evidence reference")
    path = Path(ref["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("prior-product evidence must be an absolute regular file")
    raw = path.read_bytes()
    if _sha(raw) != ref["sha256"]:
        raise ValueError("prior-product evidence SHA mismatch: " + str(path))
    return _object(raw) if parse else raw


class PriorProductGuard:
    def __init__(self, path: Path, expected_sha256: str):
        if not path.is_absolute() or not _digest(expected_sha256):
            raise ValueError("prior-product guard requires absolute path and SHA256")
        self.path = path
        self.expected_sha256 = expected_sha256
        self.products: set[str] = set()
        self.validate()

    @classmethod
    def from_environment(cls, *, required: bool = False):
        path, digest = os.environ.get(PATH_ENV), os.environ.get(SHA_ENV)
        if not path and not digest and not required:
            return None
        if not path or not digest:
            raise ValueError("prior-product guard path and SHA256 are required before execution")
        return cls(Path(path), digest)

    def validate(self) -> None:
        value = _reference({"path": str(self.path), "sha256": self.expected_sha256})
        if value.get("schema_version") != "deeptutor-prior-product-guard/v1" or value.get("task") != "deeptutor":
            raise ValueError("prior-product guard schema/task mismatch")
        if value.get("digest_algorithm") != "agentloop.protocol.tree_digest":
            raise ValueError("prior-product digest algorithm mismatch")
        if (value.get('guard_digest_algorithm') != 'deeptutor-product-files/v1'
                or value.get('guard_algorithm_sha256') != _sha(Path(__file__).read_bytes())):
            raise ValueError('loaded guard product algorithm does not match guard')
        algorithm = value.get("algorithm")
        if not isinstance(algorithm, dict) or _sha(Path(protocol.__file__).read_bytes()) != algorithm.get("sha256"):
            raise ValueError("loaded product digest algorithm does not match guard")
        _reference(algorithm, parse=False)
        runs = value.get("runs")
        products = value.get("products")
        if not isinstance(runs, list) or not isinstance(products, list):
            raise ValueError("prior-product guard must bind runs and products")
        if (not runs or not products) and not (value.get("fresh_install") is True and not runs and not products):
            # A fresh installation has dispatched no products; it must say so explicitly.
            raise ValueError("prior-product guard must bind runs and products")
        run_paths = set()
        carried_runs = set()
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("run_dir"), str) or run["run_dir"] in run_paths:
                raise ValueError("invalid or duplicate prior run")
            run_paths.add(run["run_dir"])
            if run.get("provenance") == "carried-from-sealed-guard":
                # The run directory was destroyed. Its products stay blocked on
                # the authority of the sealed guard that verified them while the
                # evidence existed; that guard must still be present and intact.
                if Path(run["run_dir"]).exists():
                    raise ValueError("carried prior run must not have a live evidence directory")
                _reference(run.get("sealed_guard"), parse=False)
                carried_runs.add(run["run_dir"])
                continue
            ledger = _reference(run.get("ledger"))
            _reference(run.get("summary"))
            _reference(run.get("cleanup"))
            requests = ledger.get("logical_requests")
            if not isinstance(requests, dict) or ledger.get("calls") != run.get("calls"):
                raise ValueError("prior ledger identity/call count mismatch")
            if any(not _digest(k) or not isinstance(v, dict) for k, v in requests.items()):
                raise ValueError("malformed prior request ledger")
            unknown = [{"request_sha256": k, "context_id": v.get("context_id"), "state": v.get("state"),
                        "transport_attempts": v.get("transport_attempts"), "result": v.get("result")}
                       for k, v in requests.items() if v.get("state") != "completed"]
            if unknown != run.get("unknown_requests"):
                raise ValueError("prior unknown ledger binding mismatch")
            for field in ("ledger", "summary", "cleanup"):
                if not Path(run[field]["path"]).is_relative_to(Path(run["run_dir"])):
                    raise ValueError("prior run evidence path mismatch")
        found = set()
        stable = set()
        carried = set()
        for row in products:
            if not isinstance(row, dict) or not _digest(row.get("product_digest")) or row.get("run_dir") not in run_paths:
                raise ValueError("invalid prior product identity")
            digest = row["product_digest"]
            if digest in found or row.get("recomputed_product_digest") != digest:
                raise ValueError("duplicate or unreconciled prior product")
            if row["run_dir"] in carried_runs:
                # Blocked on the sealed guard's authority; the attempt file that
                # proved it is gone with its run directory. Identity is still
                # required to be internally consistent.
                if row.get("provenance") != "carried-from-sealed-guard":
                    raise ValueError("product of a carried run must declare carried provenance")
                if not isinstance(row.get("product_path"), str) or not row.get("state"):
                    raise ValueError("carried prior product lacks identity")
            else:
                attempt = _reference(row.get("attempt"))
                record = attempt.get("record")
                if (attempt.get("candidate_digest") != digest or attempt.get("state") != row.get("state")
                        or not isinstance(record, dict) or record.get("candidate_digest") != digest
                        or record.get("candidate_path") != row.get("product_path")):
                    raise ValueError("prior attempt/product identity mismatch")
                attempt_path = Path(row["attempt"]["path"])
                if attempt_path != Path(row["run_dir"]) / "public_attempts" / (digest + ".json"):
                    raise ValueError("prior attempt path mismatch")
            found.add(digest)
            if not _digest(row.get('guard_product_digest')):
                raise ValueError('missing stable product identity')
            stable.add(row['guard_product_digest'])
            if row["run_dir"] in carried_runs:
                carried.add(row['guard_product_digest'])
        self.products = stable
        # Visible to any caller that wants to know how much of the blocking list
        # rests on the indirect chain rather than on evidence it can open.
        self.carried_products = carried

    def blocks(self, repository: Path) -> bool:
        if not repository.is_dir() or repository.is_symlink():
            raise ValueError("invalid materialized product directory")
        self.validate()
        return product_digest(repository) in self.products

    def binding(self) -> dict[str, Any]:
        return {"path": str(self.path), "sha256": self.expected_sha256,
                "digest_algorithm": "deeptutor-product-files/v1", "product_count": len(self.products)}
