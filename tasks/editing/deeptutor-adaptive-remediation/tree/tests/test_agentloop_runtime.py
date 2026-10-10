from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from agentloop.protocol import (
    BUILDER_EFFORT,
    BUILDER_MODEL,
    HIDDEN_CASES,
    PLACEHOLDER_TOKEN,
    STATS_TOKEN,
    tree_digest,
)
from agentloop.run_hidden import run as run_hidden
from agentloop.two_round_controller import TwoRoundController


def _candidate(root: Path, marker: str) -> Path:
    path = root / marker
    path.mkdir()
    (path / "candidate.txt").write_text(marker, encoding="utf-8")
    return path


def _terminal_results() -> dict[str, dict[str, object]]:
    return {
        "dev_001": {"classification": "candidate_capability_gap", "terminal": True},
        "dev_002": {"classification": "candidate_capability_gap", "terminal": True},
    }


def _builder_witness(session_id: str = "builder-session-1") -> dict[str, object]:
    return {
        "session_id": session_id,
        "connection_id": "evaluator-connection-001",
        "single_connection": True,
        "transport": "evaluator-owned-single-session",
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "started_at": "2026-09-03T00:00:00+00:00",
    }


class _StatsHandler(BaseHTTPRequestHandler):
    calls = 0
    lock = threading.Lock()

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/stats" or self.headers.get("Authorization") != f"Bearer {STATS_TOKEN}":
            self.send_response(404)
            self.end_headers()
            return
        with self.lock:
            observed = self.__class__.calls
        payload = json.dumps(
            {
                "schema_version": "agentswe-broker-stats/v1",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "calls": observed,
                "successful_calls": observed,
                "failures": 0,
                "provider_failures": 0,
                "delivery_failures": 0,
                "input_tokens": 10 * observed,
                "output_tokens": 5 * observed,
                "total_tokens": 15 * observed,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/responses" or self.headers.get("Authorization") != f"Bearer {PLACEHOLDER_TOKEN}":
            self.send_response(403)
            self.end_headers()
            return
        with self.lock:
            self.__class__.calls += 1
        payload = json.dumps({"status": "completed"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class AgentLoopRuntimeTests(unittest.TestCase):
    def test_accepted_submission_ledger_allows_three_rounds_and_freezes_latest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
            witness = _builder_witness()
            controller = TwoRoundController(
                tmp_path / "ledger", "builder-session-1",
                builder_witness=witness, max_dev_rounds=3,
            )
            candidates = [_candidate(tmp_path, f"candidate-{index}") for index in range(1, 4)]
            previous_feedback = None
            for index, candidate in enumerate(candidates, start=1):
                record = controller.submit(
                    candidate, index, _terminal_results(), feedback=previous_feedback,
                    builder_session_id="builder-session-1", builder_witness=witness,
                )
                self.assertEqual(record["round"], index)
                previous_feedback = controller.feedback_record
            self.assertEqual(len(controller.records), 3)
            self.assertTrue(all(item["dev_passed"] is False for item in controller.records))
            frozen = controller.freeze()
            self.assertEqual(frozen["source_submission"], 3)
            self.assertEqual(frozen["accepted_submission_count"], 3)
            self.assertTrue(frozen["feedback_received"])
            self.assertTrue(frozen["feedback_chain_consumed"])
            self.assertTrue(frozen["same_session_verified"])
            self.assertEqual(tree_digest(Path(frozen["candidate_path"])), frozen["candidate_digest"])

    def test_two_round_controller_requires_feedback_and_freezes_private_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
            c1 = _candidate(tmp_path, "candidate-1")
            c2 = _candidate(tmp_path, "candidate-2")
            witness = _builder_witness()
            controller = TwoRoundController(
                tmp_path / "controller",
                "builder-session-1",
                builder_witness=witness,
            )

            first = controller.submit(
                c1,
                1,
                _terminal_results(),
                builder_session_id="builder-session-1",
                builder_witness=witness,
            )
            self.assertEqual(first["session_id"], "builder-session-1")
            self.assertTrue((tmp_path / "controller/feedback_after_candidate_1.json").is_file())

            with self.assertRaisesRegex(RuntimeError, "requires evaluator feedback"):
                controller.submit(
                    c2,
                    2,
                    _terminal_results(),
                    builder_session_id="builder-session-1",
                    builder_witness=witness,
                )
            with self.assertRaisesRegex(RuntimeError, "different Builder session"):
                controller.submit(
                    c2,
                    2,
                    _terminal_results(),
                    feedback={"source_submission": 1},
                    builder_session_id="other-session",
                    builder_witness=_builder_witness("other-session"),
                )

            feedback = json.loads(
                (tmp_path / "controller/feedback_after_candidate_1.json").read_text()
            )
            second = controller.submit(
                c2,
                2,
                _terminal_results(),
                feedback=feedback,
                builder_session_id="builder-session-1",
                builder_witness=witness,
            )
            frozen = controller.freeze()
            frozen_path = Path(frozen["candidate_path"])
            self.assertEqual(frozen["source_submission"], 2)
            self.assertTrue(frozen["feedback_received"])
            self.assertTrue(frozen["same_session_verified"])
            self.assertTrue(frozen["frozen_tree_read_only"])
            self.assertTrue(frozen["frozen_tree_regular"])
            self.assertEqual(frozen["candidate_digest"], second["candidate_digest"])
            self.assertEqual(tree_digest(frozen_path), second["candidate_digest"])

            (c2 / "builder_later_edit.txt").write_text("must not enter hidden", encoding="utf-8")
            self.assertEqual(tree_digest(frozen_path), second["candidate_digest"])
            with self.assertRaisesRegex(RuntimeError, "never emits a fake"):
                controller.hidden()

    def test_hidden_executor_preserves_six_synthetic_cases_without_claiming_real_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
            frozen = tmp_path / "frozen"
            frozen.mkdir()
            (frozen / "candidate.txt").write_text("frozen", encoding="utf-8")
            freeze = tmp_path / "freeze_manifest.json"
            for item in frozen.rglob("*"):
                item.chmod(item.stat().st_mode & ~0o222)
            frozen.chmod(frozen.stat().st_mode & ~0o222)
            freeze.write_text(
                json.dumps(
                    {
                        "schema_version": "agentswe-deeptutor-freeze/v1",
                        "source_submission": 2,
                        "session_id": "builder-session-1",
                        "builder_witness_digest": "a" * 64,
                        "candidate_digest": tree_digest(frozen),
                        "candidate_path": str(frozen),
                        "feedback_received": True,
                        "same_session_verified": True,
                        "hidden_allowed": True,
                        "frozen_tree_read_only": True,
                        "frozen_tree_regular": True,
                        "frozen_at": "2026-09-02T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            fake_launcher = tmp_path / "fake_launcher.py"
            fake_launcher.write_text(
                """
import argparse, json
from pathlib import Path
from urllib.request import Request, urlopen
p = argparse.ArgumentParser()
p.add_argument('--repository'); p.add_argument('--source-repository'); p.add_argument('--prompt'); p.add_argument('--output')
p.add_argument('--broker-endpoint'); p.add_argument('--execute', action='store_true'); p.add_argument('--python')
a = p.parse_args()
out = Path(a.output)
import shutil
shutil.copytree(a.source_repository, a.repository)
request = Request(a.broker_endpoint, data=b'{}', method='POST', headers={'Authorization':'Bearer broker-only-placeholder','Content-Type':'application/json'})
with urlopen(request, timeout=5) as response:
    response.read()
(out / 'agent_result.json').write_text(json.dumps({'schema_version':'test-artifact/v1'}))
(out / 'launcher_result.json').write_text(json.dumps({'schema_version':'test-launch/v1','executed':True,'classification':'candidate_capability_gap'}))
""",
                encoding="utf-8",
            )
            _StatsHandler.calls = 0
            server = ThreadingHTTPServer(("127.0.0.1", 0), _StatsHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/responses"
                attestation = run_hidden(
                    freeze,
                    tmp_path / "hidden",
                    broker_endpoint=endpoint,
                    launcher=fake_launcher,
                    python_executable="python3",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(attestation["expected_cases"], list(HIDDEN_CASES))
            self.assertEqual(attestation["executed_cases"], list(HIDDEN_CASES))
            self.assertFalse(attestation["all_cases_real"])
            self.assertTrue(attestation["broker_stats_present"])
            self.assertFalse(attestation["formal_result_published"])
            self.assertFalse(attestation["formal_result_eligible"])
            for case_id in HIDDEN_CASES:
                case_dir = tmp_path / "hidden/cases" / case_id
                self.assertTrue((case_dir / "candidate_runtime").is_dir())
                self.assertTrue((case_dir / "broker_before.json").is_file())
                self.assertTrue((case_dir / "broker_after.json").is_file())
                self.assertTrue((case_dir / "broker_delta.json").is_file())
                self.assertTrue((case_dir / "case_result.json").is_file())
                result = json.loads((case_dir / "case_result.json").read_text())
                self.assertEqual(result["classification"], "candidate_capability_gap")
                self.assertFalse(result["environment_preflight"]["valid"])
                self.assertTrue(result["launcher_executed"])
                self.assertTrue(result["frozen_candidate_stable"])

    def test_hidden_executor_rejects_unfrozen_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
            manifest = tmp_path / "bad.json"
            manifest.write_text(
                json.dumps({"source_submission": 1, "hidden_allowed": False}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Candidate 2 freeze"):
                run_hidden(
                    manifest,
                    tmp_path / "hidden",
                    broker_endpoint="http://127.0.0.1:1/v1/responses",
                )


if __name__ == "__main__":
    unittest.main()
