import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
import result_judge as judge


class Response:
    def __init__(self, body=None, status=200):
        self.body = body
        self.status_code = status
        self.headers = {}
        self.text = json.dumps(body)
        self.raw = io.BytesIO(self.text.encode())

    def close(self):
        pass

    def json(self):
        if isinstance(self.body, str):
            raise ValueError("not JSON")
        return self.body


class JudgeTransportTests(unittest.TestCase):
    def body(self, **changes):
        return {"status": "completed", "model": "deepseek-flash", "id": "mock-response",
                "output_text": "{}", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, **changes}

    def call(self):
        return judge.call_judge("test", "placeholder", 900, 4, endpoint="http://127.0.0.1:1/v1/responses")

    def test_completed_response_once(self):
        with patch.object(judge.requests, "post", return_value=Response(self.body())) as post:
            result = self.call()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result[1], 1)

    def test_only_explicit_retryable_http_is_retried(self):
        with patch.object(judge.requests, "post", side_effect=[Response(status=502), Response(self.body())]) as post, \
                patch.object(judge.time, "sleep"):
            result = self.call()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result[1], 2)

    def test_invalid_delivered_responses_never_resampled(self):
        for body in ("bad JSON", self.body(output_text="", output=[]),
                     self.body(status="incomplete"), self.body(model="wrong")):
            with self.subTest(body=body), patch.object(judge.requests, "post", return_value=Response(body)) as post:
                with self.assertRaises(judge.ReceivedResponseFailure):
                    self.call()
                self.assertEqual(post.call_count, 1)

    def test_nonretryable_http_and_read_timeout_do_not_retry(self):
        for response in (Response(status=400), Response(status=401)):
            with patch.object(judge.requests, "post", return_value=response) as post:
                with self.assertRaises(judge.TransportFailure):
                    self.call()
                self.assertEqual(post.call_count, 1)
        with patch.object(judge.requests, "post", side_effect=requests.ReadTimeout()) as post:
            with self.assertRaises(judge.TransportFailure):
                self.call()
            self.assertEqual(post.call_count, 1)

    def test_completed_empty_response_preserves_usage(self):
        with patch.object(judge.requests, "post", return_value=Response(self.body(output_text=""))):
            with self.assertRaises(judge.ReceivedResponseFailure) as raised:
                self.call()
        self.assertTrue(raised.exception.completed)
        self.assertEqual(raised.exception.usage["total_tokens"], 15)

    def test_task_dimensions_are_strict(self):
        dims = {"recovery": 40, "learning": 20, "evidence": 20, "report": 20}
        value = {"case_id": "test_001", "result_state": "scoreable", "result_score": 40,
                 "dimensions": {k: {"score": 10, "max": v, "evidence": "observed"} for k, v in dims.items()},
                 "major_errors": [], "assessment": "partial"}
        verified, errors = judge.validate_response(json.dumps(value), "test_001", dims)
        self.assertFalse(errors)
        self.assertEqual(verified["result_score"], 40)
        value["dimensions"]["recovery"]["max"] = 50
        self.assertTrue(judge.validate_response(json.dumps(value), "test_001", dims)[1])
        value["result_state"] = "fatal_candidate_failure"
        self.assertTrue(judge.validate_response(json.dumps(value), "test_001", dims)[1])

    def test_dynamic_cli_contract_and_completed_attempt_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {}
            for key in ("task-input", "rubric", "agent-artifact", "trajectory", "native-evidence", "oracle-summary"):
                paths[key] = root / (key + ".txt")
                paths[key].write_text("sanitized evidence")
            dims = {"science": 30, "recovery": 25, "durability": 20, "provenance": 15, "honesty": 10}
            (root / "result_dimensions.json").write_text(json.dumps(dims))
            value = {"case_id": "dev_001", "result_state": "scoreable", "result_score": 50,
                     "dimensions": {k: {"score": 10, "max": v, "evidence": "observed"} for k, v in dims.items()},
                     "major_errors": [], "assessment": "partial"}
            args = ["result_judge", "--case-id", "dev_001", "--broker-endpoint", "http://127.0.0.1:1/v1/responses",
                    "--output-dir", str(root / "out"), '--transport-mode', 'nonstream']
            for key, path in paths.items():
                args.extend(["--" + key, str(path)])
            with patch.object(sys, "argv", args), patch.object(judge.requests, "post", return_value=Response(self.body(output_text=json.dumps(value)))) as post, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(judge.main(), 0)
                contract = root / "out/result_score_contract.json"
                first = contract.read_bytes()
                self.assertEqual(judge.main(), 2)
                self.assertEqual(contract.read_bytes(), first)
            self.assertEqual(post.call_count, 1)
            saved = json.loads(first)
            self.assertEqual(saved["dimension_maxima"], dims)
            self.assertEqual(saved["result_score"], 50)


if __name__ == "__main__":
    unittest.main()
