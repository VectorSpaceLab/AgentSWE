"""ReceiptNormalizer._directory and failed receipts of infrastructure attempts (release fix).

A failed, recovered or incomplete receipt blocks only when it is the request being normalized; a failed sibling (an
unattributed provider transient) does not. Synthetic receipts; no run directory, provider or Docker needed.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[1] / "runners" / "editing" / "control"
sys.path.insert(0, str(CONTROL))
from v2_readiness import checked_path, sha256_file, tree_digest  # noqa: E402
from v2_receipt_usage_normalizers import ReceiptNormalizer  # noqa: E402

USAGE = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}


class UnattributedFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.receipts = self.root / "raw" / "lower_requests"
        self.receipts.mkdir(parents=True)
        (self.receipts / ".process.lock").write_text("")
        self.access = {"run_id": "run-1", "read_json": self.read_json, "directory": self.directory}
        self.n = 0

    # ---- bundle access, as the exporters and v2_readiness provide it ----
    def read_json(self, ref):
        path = checked_path(self.root, ref["path"])
        if sha256_file(path) != ref["sha256"]:
            raise ValueError("artifact hash mismatch")
        return json.loads(path.read_bytes())

    def directory(self, ref):
        path = checked_path(self.root, ref["path"], directory=True)
        if tree_digest(path) != ref["tree_digest"]:
            raise ValueError("bundled receipt tree changed")
        return path

    # ---- receipts ----
    def rid(self):
        self.n += 1
        return hashlib.sha256(f"request-{self.n}".encode()).hexdigest()

    def complete(self):
        rid = self.rid()
        d = self.receipts / rid
        d.mkdir()
        response = json.dumps({"id": f"resp-{rid[:8]}", "status": "completed", "model": "deepseek-flash",
                               "error": None, "usage": USAGE}).encode()
        (d / "intent.json").write_text(json.dumps({"identity": rid, "protocol": "agentswe-lower-single-upstream/v1",
                                                   "body_sha256": hashlib.sha256(rid.encode()).hexdigest()}))
        (d / "upstream_started.json").write_text(json.dumps({"actual_upstream_requests": 1, "identity": rid}))
        (d / "response.bin").write_bytes(response)
        (d / "completed.json").write_text(json.dumps({
            "identity": rid, "status": 200, "actual_upstream_requests": 1, "completed_responses": 1,
            "payload_sha256": hashlib.sha256(response).hexdigest(), "content_type": "application/json",
            "usage": USAGE}))
        return rid

    def failed(self):
        """What a lower broker leaves for a provider RemoteDisconnected: intent, start, failure.json."""
        rid = self.rid()
        d = self.receipts / rid
        d.mkdir()
        (d / "intent.json").write_text(json.dumps({"identity": rid, "protocol": "agentswe-lower-single-upstream/v1",
                                                   "body_sha256": hashlib.sha256(rid.encode()).hexdigest()}))
        (d / "upstream_started.json").write_text(json.dumps({"actual_upstream_requests": 1, "identity": rid}))
        (d / "failure.json").write_text(json.dumps({"actual_upstream_requests": 1, "automatic_retry_allowed": False,
                                                    "error": "RemoteDisconnected", "identity": rid,
                                                    "outcome": "unknown", "status": None, "usage": None}))
        return rid

    def index(self):
        tree = tree_digest(self.receipts)
        terminal = self.root / "terminal.json"
        terminal.write_text(json.dumps({"run_id": "run-1", "owner": "evaluator", "process_reaped": True,
                                        "state": "terminal", "receipt_tree_digest": tree}))
        return {"schema_version": "agentswe-evaluator-receipt-index/v1", "task": "codex", "owner": "evaluator",
                "run_id": "run-1",
                "receipt_directory": {"path": self.receipts.relative_to(self.root).as_posix(), "tree_digest": tree},
                "broker_terminal": {"path": "terminal.json", "sha256": sha256_file(terminal)}}

    def normalize(self, rid):
        return ReceiptNormalizer("codex").normalize_with_artifacts(self.index(), rid, self.access)

    # ---- cases ----
    def test_unattributed_failed_sibling_does_not_block(self):
        a, b = self.complete(), self.complete()
        self.failed()
        self.assertTrue(self.normalize(a))
        self.assertTrue(self.normalize(b))

    def test_attributed_failed_request_still_blocks(self):
        self.complete()
        f = self.failed()
        with self.assertRaisesRegex(ValueError, "request incomplete, failed, recovered"):
            self.normalize(f)

    def test_attributed_incomplete_request_still_blocks(self):
        self.complete()
        rid = self.complete()
        (self.receipts / rid / "completed.json").unlink()
        with self.assertRaisesRegex(ValueError, "request incomplete, failed, recovered"):
            self.normalize(rid)

    def test_attributed_recovered_request_still_blocks(self):
        self.complete()
        rid = self.complete()
        (self.receipts / rid / "recovered_interruption.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "request incomplete, failed, recovered"):
            self.normalize(rid)

    def test_complete_siblings_still_validated(self):
        a, b = self.complete(), self.complete()
        # a complete-looking sibling with a broken payload binding still blocks every request
        (self.receipts / b / "response.bin").write_bytes(b'{"tampered": true}')
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.normalize(a)

    def test_reused_response_id_across_siblings_still_blocks(self):
        a, b = self.complete(), self.complete()
        body = (self.receipts / a / "response.bin").read_bytes()
        (self.receipts / b / "response.bin").write_bytes(body)
        done = json.loads((self.receipts / b / "completed.json").read_text())
        done["payload_sha256"] = hashlib.sha256(body).hexdigest()
        (self.receipts / b / "completed.json").write_text(json.dumps(done))
        with self.assertRaisesRegex(ValueError, "reused"):
            self.normalize(a)


if __name__ == "__main__":
    unittest.main()
