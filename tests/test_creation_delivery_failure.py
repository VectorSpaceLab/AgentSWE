"""Creation runtime health: a case whose only broker failures are delivery_failure is judged, not withheld (stdlib
unittest; a local HTTP server stands in for the Candidate broker's events endpoint).

The paper's evaluation policy withholds and replays cases with broker/provider failures, but judges a case whose only failures
are delivery_failure (the Candidate itself hung up, for example at its own timeout) on what the Candidate produced."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runners" / "creation"))

import runtime_contract  # noqa: E402


class Broker(BaseHTTPRequestHandler):
    events: list = []

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DeliveryFailure(unittest.TestCase):
    def run_health(self, events):
        Broker.events = events
        server = HTTPServer(("127.0.0.1", 0), Broker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        jobs, run_dir = root / "jobs", root / "run-dev-r001-a001"
        (jobs / "job").mkdir(parents=True)
        (jobs / "job" / "result.json").write_text("{}")
        run_dir.mkdir()
        config = root / "candidate_job_config.json"
        config.write_text(json.dumps({"jobs_dir": str(jobs), "job_name": "job"}))
        saved = {k: os.environ.get(k) for k in ("AGENTSWE_CANDIDATE_BROKER_PORT", "AGENTSWE_BROKER_HOST")}
        os.environ.update({"AGENTSWE_CANDIDATE_BROKER_PORT": str(server.server_address[1]),
                           "AGENTSWE_BROKER_HOST": "127.0.0.1"})
        try:
            runtime_contract.assert_runtime_health(config, run_dir)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return run_dir

    @staticmethod
    def event(case, classification):
        return {"case_id": case, "request_id": case + classification, "classification": classification}

    def test_delivery_only_case_is_judged_not_withheld(self):
        run_dir = self.run_health([self.event("dev_002", "delivery_failure"), self.event("dev_001", "success")])
        self.assertFalse((run_dir / "infra_failed_cases.json").exists())
        self.assertEqual(json.loads((run_dir / "candidate_delivery_failures.json").read_text()), ["dev_002"])
        self.assertEqual(len(json.loads((run_dir / "broker_infrastructure_failures.json").read_text())), 1)

    def test_provider_failure_still_withholds(self):
        run_dir = self.run_health([self.event("dev_001", "provider_failure"), self.event("dev_002", "delivery_failure")])
        self.assertEqual(json.loads((run_dir / "infra_failed_cases.json").read_text()), ["dev_001"])
        self.assertEqual(json.loads((run_dir / "candidate_delivery_failures.json").read_text()), ["dev_002"])

    def test_mixed_failures_in_one_case_withhold_it(self):
        run_dir = self.run_health([self.event("dev_001", "delivery_failure"), self.event("dev_001", "transport_failure")])
        self.assertEqual(json.loads((run_dir / "infra_failed_cases.json").read_text()), ["dev_001"])
        self.assertFalse((run_dir / "candidate_delivery_failures.json").exists())


if __name__ == "__main__":
    unittest.main()
