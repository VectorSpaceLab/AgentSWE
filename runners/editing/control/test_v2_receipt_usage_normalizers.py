"""Provider-free tests run actual receipt writers and staged Claude lifecycle."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from v2_readiness import checked_path, sha256_file, tree_digest
from v2_usage_normalizers import make_broker_record_normalizers

ROOT = Path(__file__).resolve().parent
CURRENT = ROOT / "v2-current-controller-stage/current"
TASKS = {"aider": "12-edit-aider-worktree-transaction-agentloop-v1",
         "codex": "15-edit-codex-execution-residual-agentloop-v5",
         "claude": "11-edit-claude-policy-provenance-v3-agentloop-v1"}
USAGE = dict(input_tokens=2, output_tokens=1, total_tokens=3)


def load(path, name, dependencies):
    sys.path.insert(0, str(dependencies))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.access = {"read_json": self.read_json, "read_bytes": self.read_bytes, "directory": self.directory, "run_id": "fresh"}

    def ref(self, path):
        return dict(path=path.relative_to(self.root).as_posix(), sha256=sha256_file(path))

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return self.ref(path)

    def read_bytes(self, ref):
        path = checked_path(self.root, ref["path"])
        if sha256_file(path) != ref["sha256"]:
            raise ValueError("artifact hash mismatch")
        return path.read_bytes()

    def read_json(self, ref):
        return json.loads(self.read_bytes(ref))

    def directory(self, ref):
        path = checked_path(self.root, ref["path"], directory=True)
        if tree_digest(path) != ref["tree_digest"]:
            raise ValueError("directory changed")
        return path

    def receipt_fixture(self, task, *, pending=False, sse=False):
        path = CURRENT / TASKS[task] / "evaluator/broker/lower_request_ledger.py"
        module = load(path, "actual_ledger_" + task, path.parent)
        directory = self.root / (task + "-receipts")
        ledger = module.RequestLedger(directory)
        self.addCleanup(ledger.close)
        request, _, error = ledger.claim({"model": "deepseek-flash", "input": "provider-free fixture"}, "/v1/responses", "https://provider.invalid")
        self.assertIsNone(error)
        ledger.sent(request)
        if not pending:
            response = dict(id="actual-response-fixture", status="completed", model="deepseek-flash", error=None, usage=USAGE)
            payload = json.dumps(response).encode()
            kind = "application/json"
            if sse:
                payload = ("event: response.completed\ndata: " + json.dumps(dict(type="response.completed", response=response)) + "\n\n").encode()
                kind = "text/event-stream"
            ledger.complete(request, payload=payload, status=200, content_type=kind, usage=USAGE)
        digest = tree_digest(directory)
        terminal = self.write(task + "-terminal.json", dict(run_id="fresh", owner="evaluator", process_reaped=True,
            state="terminal", receipt_tree_digest=digest))
        index = dict(schema_version="agentswe-evaluator-receipt-index/v1", task=task, run_id="fresh", owner="evaluator",
            receipt_directory=dict(path=directory.name, tree_digest=digest), broker_terminal=terminal)
        return index, request.name, request

    def normalize(self, task, index, request_id):
        return make_broker_record_normalizers(task)["public_lower"].normalize_with_artifacts(index, request_id, self.access)

    def test_real_aider_codex_receipts_and_original_response(self):
        for task in ("aider", "codex"):
            index, rid, _ = self.receipt_fixture(task)
            self.assertEqual(self.normalize(task, index, rid), dict(request_id=rid, state="success", upstream_attempts=1, known_tokens=3, usage_known=True))

    def test_real_sse_receipt(self):
        index, rid, _ = self.receipt_fixture("aider", sse=True)
        self.assertEqual(self.normalize("aider", index, rid)["known_tokens"], 3)

    def test_actual_pending_request_not_inferred_complete(self):
        index, rid, _ = self.receipt_fixture("codex", pending=True)
        with self.assertRaises(ValueError):
            self.normalize("codex", index, rid)

    def test_raw_response_tampering_and_stats_only_rejected(self):
        index, rid, path = self.receipt_fixture("aider")
        (path / "response.bin").write_text("tampered")
        with self.assertRaises(ValueError):
            self.normalize("aider", index, rid)
        with self.assertRaises(ValueError):
            make_broker_record_normalizers("aider")["public_lower"]({"calls": 1, "known_tokens": 3}, rid)

    def test_reanchored_response_must_still_match_completion(self):
        index, rid, path = self.receipt_fixture("aider")
        (path / "response.bin").write_text("{}")
        index["receipt_directory"]["tree_digest"] = tree_digest(path.parent)
        with self.assertRaises(ValueError):
            self.normalize("aider", index, rid)

    def test_reanchored_inner_symlink_rejected_before_read(self):
        index, rid, path = self.receipt_fixture("aider")
        response = path / "response.bin"
        response.rename(self.root / "outside-response.bin")
        response.symlink_to(self.root / "outside-response.bin")
        index["receipt_directory"]["tree_digest"] = tree_digest(path.parent)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.normalize("aider", index, rid)

    def test_actual_receipt_missing_response_identity_rejected(self):
        index, rid, path = self.receipt_fixture("codex")
        response = json.loads((path / "response.bin").read_text())
        response.pop("id")
        data = json.dumps(response).encode()
        (path / "response.bin").write_bytes(data)
        completed = json.loads((path / "completed.json").read_text())
        completed["payload_sha256"] = hashlib.sha256(data).hexdigest()
        (path / "completed.json").write_text(json.dumps(completed))
        index["receipt_directory"]["tree_digest"] = tree_digest(path.parent)
        with self.assertRaisesRegex(ValueError, "ID missing"):
            self.normalize("codex", index, rid)

    def test_duplicate_json_key_rejected(self):
        index, rid, path = self.receipt_fixture("codex")
        (path / "upstream_started.json").write_text('{"identity":"' + rid + '","actual_upstream_requests":0,"actual_upstream_requests":1}')
        index["receipt_directory"]["tree_digest"] = tree_digest(path.parent)
        with self.assertRaises(ValueError):
            self.normalize("codex", index, rid)

    def test_receipt_termination_and_request_inventory_required(self):
        index, rid, path = self.receipt_fixture("codex")
        index["broker_terminal"] = self.write("codex-terminal.json", dict(run_id="fresh", owner="evaluator", process_reaped=False,
            state="running", receipt_tree_digest=index["receipt_directory"]["tree_digest"]))
        with self.assertRaises(ValueError):
            self.normalize("codex", index, rid)
        (path.parent / ("b" * 64)).mkdir()
        index["receipt_directory"]["tree_digest"] = tree_digest(path.parent)
        with self.assertRaises(ValueError):
            self.normalize("codex", index, rid)

    def claude_fixture(self, *, original=False, running=False):
        source = CURRENT / TASKS["claude"] / "agentloop/evaluator/broker.py"
        path = source if original else ROOT / "v2-receipt-stage/claude_broker.py"
        module = load(path, "claude_original" if original else "claude_staged", source.parent)
        stats_path = self.root / "claude-stats.json"
        state = module.BrokerState() if original else module.BrokerState(stats_path)
        if not original:
            state.handler_started()
        state.record(failed=False, status_code=200, usage=USAGE, upstream_attempted=True, response_id="response-claude-1")
        if not original and not running:
            state.handler_finished(); state.closed()
        if original:
            stats_path.write_text(json.dumps(state.stats()))
        terminal = self.write("claude-terminal.json", dict(run_id="fresh", owner="evaluator", process_reaped=True,
            state="terminal", stats_sha256=sha256_file(stats_path)))
        index = dict(schema_version="agentswe-evaluator-receipt-index/v1", task="claude", run_id="fresh", owner="evaluator",
                     broker_stats=self.ref(stats_path), broker_terminal=terminal)
        return state, index

    def test_actual_staged_claude_final_lifecycle(self):
        _, index = self.claude_fixture()
        self.assertEqual(self.normalize("claude", index, "response-claude-1")["known_tokens"], 3)

    def test_claude_component_token_drift_rejected(self):
        _, index = self.claude_fixture()
        raw = self.read_json(index["broker_stats"])
        raw["runtime"].update(input_tokens=1, output_tokens=2)
        index["broker_stats"] = self.write("claude-stats.json", raw)
        index["broker_terminal"] = self.write("claude-terminal.json", dict(run_id="fresh", owner="evaluator", process_reaped=True,
            state="terminal", stats_sha256=index["broker_stats"]["sha256"]))
        with self.assertRaisesRegex(ValueError, "component"):
            self.normalize("claude", index, "response-claude-1")

    def test_original_claude_aggregate_cannot_claim_no_inflight(self):
        _, index = self.claude_fixture(original=True)
        with self.assertRaises(ValueError):
            self.normalize("claude", index, "response-claude-1")

    def test_actual_claude_started_unfinished_handler_rejected(self):
        state, index = self.claude_fixture(running=True)
        with self.assertRaises(RuntimeError):
            state.closed()
        with self.assertRaises(ValueError):
            self.normalize("claude", index, "response-claude-1")

    def test_actual_claude_server_close_joins_handler_before_final_stats(self):
        source = CURRENT / TASKS["claude"] / "agentloop/evaluator/broker.py"
        module = load(ROOT / "v2-receipt-stage/claude_broker.py", "claude_join_test", source.parent)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        class DelayedHandler(module.Handler):
            def _do_POST(handler):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture release timed out")
                handler.state.record(failed=True, failure_classification="protocol_failure", status_code=400)
                handler.send_json(200, {"provider_free": True})
        server = module.BrokerServer(("127.0.0.1", 0), DelayedHandler)
        stats_path = self.root / "joined-stats.json"
        server.state = module.BrokerState(stats_path)
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        errors = []
        def client():
            try:
                with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{server.server_port}", data=b"{}"), timeout=5) as response:
                    response.read()
            except Exception as exc:
                errors.append(exc)
        caller = threading.Thread(target=client)
        caller.start()
        self.assertTrue(entered.wait(3))
        self.assertEqual(server.state.stats()["lifecycle"]["in_flight"], 1)
        server.shutdown()
        def close():
            server.server_close(); closed.set()
        closer = threading.Thread(target=close)
        closer.start()
        self.assertFalse(closed.wait(0.05))
        release.set()
        caller.join(3); closer.join(3); serving.join(3)
        self.assertFalse(errors)
        self.assertTrue(closed.is_set())
        saved = json.loads(stats_path.read_text())
        self.assertEqual(saved["lifecycle"]["state"], "closed")
        self.assertEqual(saved["lifecycle"]["in_flight"], 0)

    def test_all_ten_factory_supported_explicitly(self):
        for task in ("claude", "aider", "openhands", "openclaw", "codex", "ai-scientist", "deepcode", "deeptutor", "dyad", "openwiki"):
            self.assertEqual(set(make_broker_record_normalizers(task)), {"public_lower", "hidden_lower", "result_judge", "code_judge"})


if __name__ == "__main__":
    unittest.main()
