"""Result-judge context overflow fallback of the repository-bug-repair and database-analytics evals (stdlib unittest).

    python3 -m unittest tests/test_creation_judge_fallback.py      (no model calls; the judge endpoint is faked)

A held-out repository-bug-repair case asked the judge for 1,360,700 input tokens plus 100,000 output tokens against a
1,048,576-token context: about 953 K NUL characters from binary files the candidate kept under its output directory,
and three copies of one strace audit inside the harness evidence. The provider answered HTTP 400, the eval recorded an
infrastructure error, and the run ended without a score. The first judge request still carries the full prompt,
byte for byte; only a 400 that says the context was exceeded (or a 400 with no provider message while the prompt's
upper token estimate is above the budget) builds one reduced prompt: (i) repeated harness values become references,
(ii) binary files become path/size/sha256 markers, (iii) files that are neither final artifacts nor listed in
run_report.json become the same markers, re-estimating after each step. It is sent once. eval_result.json records
evidence_reduced, a non-scoring prompt_reduction receipt and redacted 4xx body samples; verify_score.py scores the
case exactly as before.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CREATION = ROOT / "tasks" / "creation"
TASKS = ("repository-bug-repair", "database-analytics")


def overflow_body(messages: int) -> bytes:
    """DeepSeek's context-overflow 400 body, verbatim apart from the counts."""
    return ('{"error":{"message":"This model\'s maximum context length is 1048576 tokens. However, you requested '
            f'{messages + 100000} tokens ({messages} in the messages, 100000 in the completion). Please reduce the '
            'length of the messages or completion.","type":"invalid_request_error","param":null,'
            '"code":"invalid_request_error"}}').encode()


DEEPSEEK_OVERFLOW = overflow_body(1360700)  # the held-out case's request
OVERFLOW_BODY = overflow_body(20_000)  # below the fixture's estimate: the estimate alone decides when to stop
JUDGE_KEY = "judge-placeholder-token-0123456789"
RESOURCE_SECRET = "resource-placeholder-value-abcdef"


def load(task: str, rel: str):
    """Load one evaluator file without leaking its sys.path entry or sibling imports into other tests."""
    path = CREATION / task / "adapter" / "eval-template" / rel
    saved_path, saved_modules = list(sys.path), set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(f"judge_fallback_{task.replace('-', '_')}_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - saved_modules:
            sys.modules.pop(name, None)
    return module


EVALS = {task: load(task, "solution/run_eval.py") for task in TASKS}
VERIFIERS = {task: load(task, "tests/verify_score.py") for task in TASKS}
DIMENSIONS = {
    "repository-bug-repair": {"required_repair_behavior": 55, "public_regression_compatibility": 20,
                              "patch_scope_integrity": 15, "repair_execution_reporting": 10},
    "database-analytics": {"request_artifact_compliance": 8, "numerical_sql_correctness": 36,
                           "business_definition_coherence": 20, "privacy_insufficiency": 18,
                           "anomaly_auditability": 12, "chart_communication_consistency": 6},
}
DELIVERABLES = {
    "repository-bug-repair": {"solution.patch": "diff --git a/app.py b/app.py\n+fixed = True\n",
                              "repair_report.json": '{"schema_version": "1.0"}\n',
                              "migration_report.json": '{"schema_version": "1.0"}\n'},
    "database-analytics": {"answer.json": '{"status": "answered"}\n', "queries.json": "[]\n",
                           "result.csv": "a,b\n1,2\n", "chart.json": "{}\n", "dashboard.html": "<html></html>\n",
                           "decision.json": "{}\n", "lineage.json": "{}\n", "results/q1.csv": "a\n1\n"},
}


class Response:
    """A requests response: an HTTP error with a body, or a completed SSE judge stream."""

    def __init__(self, status: int, body: bytes = b"", judge: dict | None = None):
        self.status_code, self.body, self.judge = status, body, judge
        self.headers = {"Content-Type": "text/event-stream" if judge is not None else "application/json"}

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def iter_lines(self, decode_unicode=True):
        yield "data: " + json.dumps({"type": "response.output_text.delta", "delta": json.dumps(self.judge)})
        yield "data: " + json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 1}}})

    def close(self):
        pass


def judge_output(task: str, case_id: str) -> dict:
    dims = {name: {"score": maximum, "max": maximum, "evidence": "ok"} for name, maximum in DIMENSIONS[task].items()}
    return {"case_id": case_id, "evaluation_state": "scoreable", "validity_gate": True, "dimensions": dims,
            "score": 100, "major_errors": [], "assessment": "valid"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Case:
    """An eval task as the adapter stages it: manifest, active case, evaluator prompt files and candidate output."""

    def __init__(self, task: str, root: Path, *, nul_bytes: int = 200_000, notes_chars: int = 12_000):
        self.task, self.root, self.case_id = task, root, "test_002"
        audit = {"syscall_audit": "openat(AT_FDCWD, \"work/state.db\", O_RDWR) = 3\n" * 600, "exit_code": 0}
        self.harness = {"case": self.case_id, "evaluation_state": "scoreable", "validity_gate": True,
                        "public_tests": {"passed": True}, "recovery_validation": {"execution": audit, "valid": True}}
        self.harness["trusted_replay"] = copy.deepcopy(self.harness["recovery_validation"])
        contract = {"case_id": self.case_id, "validity_gate": True, "recovery_required": False,
                    "migration_valid": True, "candidate_digest": "c" * 64, "output_digest": "e" * 64,
                    "case_digest": "d" * 64, "recovery_validation": copy.deepcopy(self.harness["recovery_validation"]),
                    "trusted_harness_result": copy.deepcopy(self.harness),
                    "trusted_harness_result_sha256": digest(self.harness)}
        prefix = root / "prefix"
        (prefix / "lib" / "python3.10" / "site-packages" / "certifi").mkdir(parents=True)
        (prefix / "lib" / "python3.10" / "site-packages" / "certifi" / "cacert.pem").write_text("ca\n")
        self.manifest = {"case_id": self.case_id, "case_digest": "d" * 64, "candidate_digest": "c" * 64,
                         "candidate_output_digest": "e" * 64, "candidate_execution_contract": contract,
                         "evaluation_mode": "hidden", "container_env_prefix": str(prefix)}
        (root / "eval_manifest.json").write_text(json.dumps(self.manifest))
        (root / "cases" / self.case_id).mkdir(parents=True)
        (root / "cases" / self.case_id / "input.md").write_text("# Case\nRepair the bug.\n")
        (root / "eval_prompt.md").write_text("Judge the candidate.\n")
        (root / "rubric.md").write_text("Score only final artifacts.\n")
        (root / "agentswe.env").write_text(f"AGENTSWE_JUDGE_API_KEY={JUDGE_KEY}\nRESOURCE_TOKEN={RESOURCE_SECRET}\n")
        self.output = root / "candidate_output"
        for rel, text in DELIVERABLES[task].items():
            (self.output / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.output / rel).write_text(text)
        listed = sorted(DELIVERABLES[task]) + ["run_report.json", "/output/extra/declared.txt"]
        (self.output / "run_report.json").write_text(json.dumps({"status": "success", "artifact_paths": listed,
                                                                  "artifacts": listed}))
        (self.output / "extra").mkdir()
        (self.output / "extra" / "declared.txt").write_text("declared by run_report\n")
        work = self.output / "_agent_work"
        (work / ".git").mkdir(parents=True)
        (work / "fixture.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * nul_bytes)
        (work / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x02" + bytes(range(256)) * 4)
        (work / "notes.txt").write_text("probe log line\n" * (notes_chars // 15))
        (root / "out").mkdir()

    def run(self, responses: list[Response], **patches) -> tuple[dict, list[dict]]:
        """Run run_eval.py main() against faked judge responses; returns (eval_result, request payloads)."""
        module = EVALS[self.task]
        sent: list[dict] = []
        queue = list(responses)

        def post(url, headers=None, json=None, timeout=None, stream=None):
            sent.append(copy.deepcopy(json))
            return queue.pop(0)

        real_mkdir = pathlib.Path.mkdir

        def mkdir(path, *args, **kwargs):  # main() creates its HOME under /tmp; keep tests inside their tmp dir
            if not str(path).startswith("/tmp/harbor-"):
                return real_mkdir(path, *args, **kwargs)

        argv = ["run_eval.py", "--manifest", str(self.root / "eval_manifest.json"), "--case-root",
                str(self.root / "cases"), "--candidate-output", str(self.output), "--eval-prompt",
                str(self.root / "eval_prompt.md"), "--rubric", str(self.root / "rubric.md"), "--harness", "unused",
                "--output-dir", str(self.root / "out"), "--credential-file", str(self.root / "agentswe.env")]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
            for name in ("AGENTSWE_JUDGE_RESPONSES_URL", "AGENTSWE_JUDGE_MODEL", "AGENTSWE_JUDGE_EFFORT"):
                os.environ.pop(name, None)
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch.object(pathlib.Path, "mkdir", mkdir))
            stack.enter_context(mock.patch.object(module.requests, "post", post))
            stack.enter_context(mock.patch.object(module.time, "sleep", lambda seconds: None))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(module, name, value))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if module.main() != 0:
                raise AssertionError("run_eval.py main() did not return 0")
        return json.loads((self.root / "out" / "eval_result.json").read_text()), sent


def prompt_of(payload: dict) -> str:
    return payload["input"][0]["content"][0]["text"]


def ok(task: str) -> Response:
    return Response(200, judge=judge_output(task, "test_002"))


class Fixture(unittest.TestCase):
    task = "repository-bug-repair"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.case = Case(self.task, Path(tmp.name))
        self.module = EVALS[self.task]

    def staged_budgets(self) -> list[int]:
        """Estimated tokens of the prompt after each reduction step (all steps forced)."""
        result, sent = self.case.run([Response(400, OVERFLOW_BODY), ok(self.task)],
                                     JUDGE_INPUT_TOKEN_BUDGET=1)
        return [step["estimated_tokens"] for step in result["prompt_reduction"]["steps"] if step["applied"]]


class TriggerTest(unittest.TestCase):
    def test_context_overflow_classification(self):
        for task, module in EVALS.items():
            with self.subTest(task=task):
                budget = module.JUDGE_INPUT_TOKEN_BUDGET
                trigger = module.context_overflow(400, DEEPSEEK_OVERFLOW, 10)
                self.assertEqual(trigger, {"reason": "provider_context_length", "provider_context_tokens": 1048576,
                                           "provider_input_tokens": 1360700})
                for body in (b"", b'{"error": {"type": "upstream_error"}}',
                             b'{"error": {"type": "upstream_error", "upstream_status": 400}}'):
                    self.assertEqual(module.context_overflow(400, body, budget + 1)["reason"],
                                     "no_error_body_estimate_over_budget")
                    self.assertIsNone(module.context_overflow(400, body, budget))
                self.assertIsNone(module.context_overflow(None, None, budget + 1))
                other = b'{"error": {"message": "reasoning effort is not supported", "type": "invalid_request_error"}}'
                self.assertIsNone(module.context_overflow(400, other, budget * 10))
                for status in (401, 403, 404, 413, 422, 429, 500, 502, 503, 524):
                    self.assertIsNone(module.context_overflow(status, DEEPSEEK_OVERFLOW, budget * 10), status)
                self.assertEqual(module.context_overflow(400, b"prompt is too long: 1200000 tokens > 1000000",
                                                         1)["reason"], "provider_context_length")

    def test_budget_follows_a_smaller_stated_context(self):
        module = EVALS["repository-bug-repair"]
        self.assertEqual(module.JUDGE_INPUT_TOKEN_BUDGET, 900_000)
        self.assertEqual(module.input_budget({"provider_context_tokens": 1_048_576}), 900_000)
        self.assertEqual(module.input_budget({"provider_context_tokens": 272_000}), (272_000 - 100_000) * 95 // 100)
        self.assertEqual(module.input_budget({"provider_context_tokens": 64_000}), 0)
        self.assertEqual(module.input_budget({"provider_context_tokens": None}), 900_000)

    def test_estimate_never_counts_a_nul_or_non_ascii_byte_below_one(self):
        module = EVALS["repository-bug-repair"]
        self.assertGreaterEqual(module.estimate_tokens("\x00" * 10_000), 10_000)
        self.assertGreaterEqual(module.estimate_tokens("�" * 1000), 3000)
        self.assertGreaterEqual(module.estimate_tokens("a" * 1000), 800)
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (1280).to_bytes(4, "big") + (800).to_bytes(4, "big")
        part = {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png + b"\0" * 40).decode()}
        self.assertEqual(module.image_token_estimate([part]), -(-1280 * 800 // 750) + 85)
        self.assertEqual(module.image_token_estimate([{"image_url": "data:image/png;base64,AAAA"}]),
                         module.IMAGE_TOKEN_FALLBACK)


class ReductionStepTest(unittest.TestCase):
    def test_duplicates_become_references_to_the_shallowest_copy(self):
        module = EVALS["repository-bug-repair"]
        big = {"syscall_audit": "x" * 5000, "ok": True}
        harness = {"recovery_validation": big, "small": {"a": 1},
                   "candidate_execution_contract": {"recovery_validation": copy.deepcopy(big), "small": {"a": 1},
                                                    "trusted_harness_result": {"recovery_validation": copy.deepcopy(big)}}}
        before = json.dumps(harness, sort_keys=True)
        view, replaced = module.deduplicate(harness)
        self.assertEqual(json.dumps(harness, sort_keys=True), before)  # the input is not modified
        self.assertEqual(replaced, 2)
        self.assertEqual(view["recovery_validation"], big)
        marker = view["candidate_execution_contract"]["recovery_validation"]
        self.assertRegex(marker, r"^\[DUPLICATE OMITTED: identical to \$\.recovery_validation of this harness "
                                 r"result, sha256 [0-9a-f]{64}\]$")
        self.assertEqual(view["candidate_execution_contract"]["trusted_harness_result"]["recovery_validation"], marker)
        self.assertEqual(view["candidate_execution_contract"]["small"], {"a": 1})  # below DUPLICATE_MIN_CHARS
        self.assertIn(hashlib.sha256(json.dumps(big, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                      marker)

    def test_binary_and_non_artifact_files_become_markers(self):
        for task, module in EVALS.items():
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp:
                case = Case(task, Path(tmp))
                full = module.tree_text(case.output, ())
                stats: dict = {}
                binary = module.tree_text(case.output, (), module.file_omitter(
                    case.output, binary=True, non_artifact=False, stats=stats))
                data = (case.output / "_agent_work" / "fixture.db").read_bytes()
                self.assertIn(f"\n--- _agent_work/fixture.db ---\n[BINARY FILE OMITTED: {len(data)} bytes, "
                              f"sha256={hashlib.sha256(data).hexdigest()}]\n", binary)
                self.assertNotIn("\x00", binary)
                self.assertIn("probe log line", binary)
                self.assertEqual(stats["binary_files"], 2)
                self.assertGreater(full.count("\x00"), 200_000)
                stats = {}
                final = module.tree_text(case.output, (), module.file_omitter(
                    case.output, binary=True, non_artifact=True, stats=stats))
                self.assertRegex(final, r"\n--- _agent_work/notes\.txt ---\n\[NON-ARTIFACT FILE OMITTED: \d+ bytes, "
                                        r"sha256=[0-9a-f]{64}\]\n")
                for rel, text in DELIVERABLES[task].items():
                    self.assertIn(f"\n--- {rel} ---\n{text}", final)
                self.assertIn("declared by run_report", final)  # listed with an absolute path
                self.assertIn('"status": "success"', final)  # run_report.json itself

    def test_steps_stop_once_the_estimate_fits(self):
        module = EVALS["repository-bug-repair"]
        with tempfile.TemporaryDirectory() as tmp:
            case = Case("repository-bug-repair", Path(tmp))
            harness = case.harness | {"candidate_execution_contract": case.manifest["candidate_execution_contract"]}

            def render(view, omit):
                return module.build_prompt("p", "r", case.case_id, "i", view, case.output, (), omit)

            prompt, steps, outcome = module.reduce_judge_prompt(render, harness, case.output, 1, force_all=True)
            self.assertEqual((prompt, outcome), (None, "still_too_large"))
            names = [step["step"] for step in steps]
            self.assertEqual(names, ["deduplicate_harness", "omit_binary_files", "omit_non_artifact_files"])
            estimates = [step["estimated_tokens"] for step in steps]
            self.assertTrue(estimates[0] > estimates[1] > estimates[2], estimates)
            for budget, applied in ((estimates[0], 1), (estimates[1], 2), (estimates[2], 3)):
                prompt, steps, outcome = module.reduce_judge_prompt(render, harness, case.output, budget,
                                                                    force_all=False)
                self.assertEqual((outcome, len(steps)), ("fits", applied))
                self.assertEqual(module.estimate_tokens(prompt), budget)

    def test_nothing_to_reduce(self):
        module = EVALS["repository-bug-repair"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "solution.patch").write_text("diff\n")
            prompt, steps, outcome = module.reduce_judge_prompt(
                lambda view, omit: module.build_prompt("p", "r", "c", "i", view, out, (), omit), {"a": 1}, out, 1,
                force_all=False)
            self.assertEqual((prompt, outcome), (None, "nothing_to_reduce"))
            self.assertEqual([step["applied"] for step in steps], [False, False, False])


class MainTest(Fixture):
    def test_a_prompt_that_fits_is_sent_once_and_unchanged(self):
        result, sent = self.case.run([ok(self.task)])
        self.assertEqual(len(sent), 1)
        self.assertEqual((result["evidence_reduced"], result["prompt_reduction"], result["judge_http_errors"]),
                         (False, None, []))
        self.assertFalse((self.case.root / "out" / "prompt_reduction.json").exists())
        self.assertEqual(result["evaluation_state"], "scoreable")
        self.assertEqual(prompt_of(sent[0]), self.paper_prompt(result))
        self.assertEqual(result["provider_counts"]["gateway_text"], 1)

    def paper_prompt(self, result: dict) -> str:
        """The paper run_eval.py's prompt expression, verbatim, over the same inputs."""
        module, case_id = self.module, self.case.case_id
        harness = result["harness_result"]
        return (
            (self.case.root / "eval_prompt.md").read_text(encoding="utf-8")
            + "\n\n# Rubric\n"
            + (self.case.root / "rubric.md").read_text(encoding="utf-8")
            + f"\n\n# Active case: {case_id}\n"
            + (self.case.root / "cases" / case_id / "input.md").read_text(encoding="utf-8")
            + "\n\n# Harness result (authoritative validity evidence)\n"
            + json.dumps(harness, indent=2, sort_keys=True)
            + "\n\n# Candidate final artifacts (source is intentionally unavailable)\n"
            + module.tree_text(self.case.output, (JUDGE_KEY, RESOURCE_SECRET))
            + "\n\nReturn one JSON object only. Required shape: "
            + json.dumps({"case_id": case_id, "evaluation_state": "scoreable", "validity_gate": True,
                          "dimensions": {name: {"score": 0, "max": maximum, "evidence": ""}
                                         for name, maximum in DIMENSIONS[self.task].items()},
                          "score": 0, "major_errors": [], "assessment": ""}, sort_keys=True))

    def test_overflow_is_retried_once_with_reduced_evidence(self):
        body = OVERFLOW_BODY[:-2] + f', "debug": "Bearer {JUDGE_KEY} {RESOURCE_SECRET}"}}}}'.encode()
        result, sent = self.case.run([Response(400, body), ok(self.task)])
        self.assertEqual(len(sent), 2)
        first, second = prompt_of(sent[0]), prompt_of(sent[1])
        self.assertEqual(first, self.paper_prompt(result))
        self.assertLess(len(second), len(first))
        self.assertNotIn("\x00", second)
        self.assertIn("[BINARY FILE OMITTED:", second)
        self.assertIn("[DUPLICATE OMITTED: identical to $.recovery_validation", second)
        self.assertEqual({k: v for k, v in sent[0].items() if k != "input"},
                         {k: v for k, v in sent[1].items() if k != "input"})  # same model, effort, output limit
        self.assertEqual(result["evaluation_state"], "scoreable")
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["errors"], [])
        self.assertIs(result["evidence_reduced"], True)
        self.assertEqual(result["provider_counts"]["gateway_text"], 2)
        receipt = result["prompt_reduction"]
        self.assertEqual(json.loads((self.case.root / "out" / "prompt_reduction.json").read_text()), receipt)
        self.assertIs(receipt["non_scoring"], True)
        self.assertEqual((receipt["reason"], receipt["outcome"]), ("provider_context_length", "fits"))
        self.assertEqual((receipt["provider_context_tokens"], receipt["provider_input_tokens"]), (1048576, 20_000))
        self.assertEqual(receipt["budget_estimated_tokens"], 900_000)
        self.assertEqual(receipt["original"]["sha256"], hashlib.sha256(first.encode()).hexdigest())
        self.assertEqual(receipt["reduced"]["sha256"], hashlib.sha256(second.encode()).hexdigest())
        self.assertLessEqual(receipt["reduced"]["estimated_tokens"], 900_000)
        self.assertEqual(receipt["retry"], {"attempts": 1, "outcome": "completed", "status": 200, "error": None})
        self.assertEqual(receipt["trigger"]["status"], 400)
        text = json.dumps(result)
        self.assertNotIn(JUDGE_KEY, text)
        self.assertNotIn(RESOURCE_SECRET, text)
        self.assertIn("maximum context length is 1048576 tokens", receipt["trigger"]["body_sample"])
        self.assertEqual([(e["prompt"], e["attempt"], e["status"]) for e in result["judge_http_errors"]],
                         [("original", 1, 400)])

    def test_steps_stop_at_the_budget_and_the_estimate_drives_them(self):
        estimates = self.staged_budgets()
        for budget, steps in ((estimates[0], 1), (estimates[1], 2), (estimates[2], 3)):
            with self.subTest(budget=budget):
                (self.case.root / "out" / "prompt_reduction.json").unlink(missing_ok=True)
                result, sent = self.case.run([Response(400, OVERFLOW_BODY), ok(self.task)],
                                             JUDGE_INPUT_TOKEN_BUDGET=budget)
                receipt = result["prompt_reduction"]
                self.assertEqual(len(receipt["steps"]), steps)
                self.assertTrue(all(step["applied"] for step in receipt["steps"]))
                self.assertEqual(receipt["reduced"]["estimated_tokens"], budget)
                self.assertIs(result["evidence_reduced"], True)

    def test_a_provider_count_above_the_estimate_applies_every_step(self):
        estimates = self.staged_budgets()
        result, sent = self.case.run([Response(400, DEEPSEEK_OVERFLOW), ok(self.task)],
                                     JUDGE_INPUT_TOKEN_BUDGET=estimates[0])
        receipt = result["prompt_reduction"]
        self.assertIs(receipt["all_steps_forced"], True)
        self.assertEqual([step["applied"] for step in receipt["steps"]], [True, True, True])
        self.assertEqual(receipt["reduced"]["estimated_tokens"], estimates[2])

    def test_no_body_400_uses_the_estimate(self):
        estimate = EVALS[self.task].estimate_tokens  # the full prompt's estimate is above 200 K (NUL bytes)
        result, sent = self.case.run([Response(400, b""), ok(self.task)], JUDGE_INPUT_TOKEN_BUDGET=150_000)
        self.assertEqual(len(sent), 2)
        self.assertEqual(result["prompt_reduction"]["reason"], "no_error_body_estimate_over_budget")
        self.assertGreater(estimate(prompt_of(sent[0])), 150_000)
        (self.case.root / "out" / "prompt_reduction.json").unlink()
        result, sent = self.case.run([Response(400, b""), ok(self.task)], JUDGE_INPUT_TOKEN_BUDGET=10_000_000)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["evaluation_state"], "infrastructure_error")
        self.assertEqual(result["errors"], ["Eval Codex request failed: HTTPError:400"])
        self.assertIsNone(result["prompt_reduction"])

    def test_other_4xx_is_not_reduced_and_its_body_is_logged(self):
        for status, body in ((400, b'{"error": {"message": "unsupported reasoning effort"}}'),
                             (401, b'{"error": {"message": "invalid api key ' + JUDGE_KEY.encode() + b'"}}'),
                             (413, OVERFLOW_BODY)):
            with self.subTest(status=status):
                result, sent = self.case.run([Response(status, body)])
                self.assertEqual(len(sent), 1)
                self.assertEqual(result["evaluation_state"], "infrastructure_error")
                self.assertEqual(result["errors"], [f"Eval Codex request failed: HTTPError:{status}"])
                self.assertIsNone(result["prompt_reduction"])
                self.assertIs(result["evidence_reduced"], False)
                [logged] = result["judge_http_errors"]
                self.assertEqual((logged["status"], logged["prompt"], logged["attempt"]), (status, "original", 1))
                self.assertEqual(logged["body_bytes"], len(body))
                self.assertNotIn(JUDGE_KEY, logged["body_sample"])
                self.assertLessEqual(len(logged["body_sample"]), 2000)

    def test_5xx_and_429_are_retried_without_reduction(self):
        result, sent = self.case.run([Response(503, b"busy")] * 5)
        self.assertEqual(len(sent), 5)
        self.assertEqual(result["errors"], ["Eval Codex request failed after retries: HTTPError:503"])
        self.assertEqual((result["prompt_reduction"], result["judge_http_errors"]), (None, []))
        self.assertEqual(result["provider_counts"]["gateway_text"], 5)
        result, sent = self.case.run([Response(429, b'{"error": {"message": "rate limited"}}'), ok(self.task)])
        self.assertEqual(len(sent), 2)
        self.assertEqual(prompt_of(sent[0]), prompt_of(sent[1]))
        self.assertEqual(result["evaluation_state"], "scoreable")
        self.assertIs(result["evidence_reduced"], False)
        self.assertEqual([(e["status"], e["attempt"]) for e in result["judge_http_errors"]], [(429, 1)])

    def test_the_reduced_prompt_is_sent_once(self):
        result, sent = self.case.run([Response(400, OVERFLOW_BODY), Response(400, OVERFLOW_BODY)])
        self.assertEqual(len(sent), 2)
        self.assertEqual(result["evaluation_state"], "infrastructure_error")
        self.assertIs(result["evidence_reduced"], False)
        self.assertEqual(result["errors"], ["Eval Codex request failed: HTTPError:400",
                                            "reduced-evidence judge retry failed: Eval Codex request failed: "
                                            "HTTPError:400"])
        self.assertEqual(result["prompt_reduction"]["retry"]["outcome"], "failed")
        self.assertEqual([(e["prompt"], e["attempt"]) for e in result["judge_http_errors"]],
                         [("original", 1), ("reduced", 2)])
        self.assertEqual(result["provider_counts"]["gateway_text"], 2)

    def test_the_reduced_request_shares_the_transport_attempts(self):
        responses = [Response(503), Response(503), Response(400, OVERFLOW_BODY), Response(503), Response(503)]
        result, sent = self.case.run(responses)
        self.assertEqual(len(sent), 5)  # 3 for the full prompt, the remaining 2 for the reduced one
        self.assertEqual(result["prompt_reduction"]["trigger"]["attempt"], 3)
        self.assertEqual(result["prompt_reduction"]["retry"]["attempts"], 2)
        self.assertEqual(result["evaluation_state"], "infrastructure_error")

    def test_still_too_large_stays_an_infrastructure_error(self):
        result, sent = self.case.run([Response(400, OVERFLOW_BODY)], JUDGE_INPUT_TOKEN_BUDGET=100)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["evaluation_state"], "infrastructure_error")
        self.assertEqual(result["errors"], ["Eval Codex request failed: HTTPError:400",
                                            "judge prompt does not fit the judge context after evidence reduction: "
                                            "still_too_large"])
        receipt = result["prompt_reduction"]
        self.assertEqual((receipt["outcome"], receipt["reduced"], receipt["retry"]), ("still_too_large", None, None))
        self.assertEqual(len(receipt["steps"]), 3)
        self.assertIs(result["evidence_reduced"], False)
        self.assertTrue((self.case.root / "out" / "prompt_reduction.json").is_file())

    def test_verify_score_scores_the_case_as_without_the_new_fields(self):
        result, sent = self.case.run([Response(400, OVERFLOW_BODY), ok(self.task)])
        self.assertIs(result["evidence_reduced"], True)
        contracts = []
        for extra in (True, False):
            evaluation = dict(result)
            if not extra:
                for key in ("evidence_reduced", "prompt_reduction", "judge_http_errors"):
                    evaluation.pop(key)
            directory = self.case.root / f"verify-{extra}"
            directory.mkdir()
            (directory / "eval_result.json").write_text(json.dumps(evaluation))
            argv = ["verify_score.py", "--manifest", str(self.case.root / "eval_manifest.json"), "--eval-result",
                    str(directory / "eval_result.json"), "--harness-result",
                    str(self.case.root / "out" / "harness_result.json"), "--verifier-dir", str(directory / "v")]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(VERIFIERS[self.task].main(), 0)
            contracts.append(((directory / "v" / "score_contract.json").read_text(),
                              (directory / "v" / "reward.json").read_text()))
        self.assertEqual(contracts[0], contracts[1])
        contract = json.loads(contracts[0][0])
        self.assertEqual((contract["score"], contract["contract_valid"]), (100, True))


class DatabaseMainTest(MainTest):
    task = "database-analytics"

    def paper_prompt(self, result: dict) -> str:
        module, case_id = self.module, self.case.case_id
        return (
            (self.case.root / "eval_prompt.md").read_text(encoding="utf-8")
            + "\n\n# Rubric\n"
            + (self.case.root / "rubric.md").read_text(encoding="utf-8")
            + f"\n\n# Active case: {case_id}\n"
            + (self.case.root / "cases" / case_id / "input.md").read_text(encoding="utf-8")
            + "\n\n# Harness result (validity and scoreability evidence)\n"
            + json.dumps(module.text_evidence(result["harness_result"]), indent=2, sort_keys=True)
            + "\n\n# Candidate final artifacts (source is intentionally unavailable)\n"
            + module.tree_text(self.case.output, (JUDGE_KEY, RESOURCE_SECRET))
            + "\n\nReturn one JSON object only. Required shape: "
            + json.dumps({"case_id": case_id, "evaluation_state": "scoreable", "validity_gate": True,
                          "dimensions": {name: {"score": 0, "max": maximum, "evidence": ""}
                                         for name, maximum in DIMENSIONS[self.task].items()},
                          "score": 0, "major_errors": [], "assessment": ""}, sort_keys=True))


class ResultCountTest(unittest.TestCase):
    """`agentswe result` says how many cases the Result judge scored on reduced evidence."""

    def test_result_counts_reduced_evidence_cases(self):
        sys.path.insert(0, str(ROOT))
        try:
            from agentswe.runners import creation_harbor_v1 as creation
        finally:
            sys.path.remove(str(ROOT))
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            def phase(name: str, reduced: dict[str, bool]) -> str:
                cases = []
                for case_id, flag in reduced.items():
                    path = home / "jobs" / name / case_id / "eval_result.json"
                    path.parent.mkdir(parents=True)
                    value = {"case_id": case_id, "score": 50}
                    if flag is not None:
                        value["evidence_reduced"] = flag
                    path.write_text(json.dumps(value))
                    cases.append({"case_id": case_id, "score": 50, "eval_result": str(path)})
                summary = home / "evaluations" / name / "score_summary.json"
                summary.parent.mkdir(parents=True)
                summary.write_text(json.dumps({"cases": cases}))
                return str(summary)

            run_dir = home / "runs" / "creation" / "run"
            run_dir.mkdir(parents=True)
            (run_dir / "one_stop_summary.json").write_text(json.dumps({
                "status": "completed", "hidden_mean": 50.0, "hidden_scores": [50, 50, 50],
                "hidden_summary": phase("hidden", {"test_001": False, "test_002": True, "test_003": None}),
                "dev_lifecycle": [{"dev_mean": 50.0, "dev_summary": phase("dev-r001", {"dev_001": True,
                                                                                        "dev_002": False})}]}))
            launch = {"run_id": "run", "task": "repository-bug-repair", "family": "creation", "mode": "formal",
                      "comparable": True, "builder": {}, "run_dir": str(run_dir), "pid": 0,
                      "log": str(home / "runs" / "creation" / "run.one_stop.log"),
                      "protocol": {"heldout_cases": ["test_001", "test_002", "test_003"]},
                      "broker": {"ledger": str(home / "ledger")}}
            result = creation.result(None, launch)
            self.assertEqual(result["evidence_reduced"], {"heldout": 1, "heldout_cases": ["test_002"], "dev": 1})
            self.assertEqual(result["per_case"], {"test_001": 50, "test_002": 50, "test_003": 50})


class SharedBlockTest(unittest.TestCase):
    def test_the_fallback_is_identical_in_both_evals(self):
        pattern = re.compile(r"^# --- judge context overflow fallback .*?^# --- end judge context overflow fallback ---$",
                             re.M | re.S)
        blocks = [pattern.findall((CREATION / task / "adapter/eval-template/solution/run_eval.py").read_text())
                  for task in TASKS]
        self.assertEqual([len(found) for found in blocks], [1, 1])
        self.assertEqual(blocks[0], blocks[1])


if __name__ == "__main__":
    unittest.main()
