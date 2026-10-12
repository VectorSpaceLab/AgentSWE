"""Provider-free disk fixtures. Synthetic receipts are never admission evidence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from v2_readiness import FILES, PROFILE, ROLES, sha256_file, tree_digest, validate_evidence


def fixture_judge_output(value, input_record):
    return [] if set(value) == {"score", "assessment"} and type(value["score"]) is int and 0 <= value["score"] <= 100 and isinstance(value["assessment"], str) and value["assessment"] else ["invalid fixture output"]


VALIDATORS = {role: fixture_judge_output for role in ("result", "code")}


def fixture_broker_record(raw, request_id):
    if raw.get("request_id") != request_id:
        raise ValueError("fixture request identity mismatch")
    return raw


NORMALIZERS = {role: fixture_broker_record for role in ROLES if role != "builder"}


class Bundle:
    def __init__(self, root, binding=None):
        self.root = root
        self.run = "fixture-run"
        self.binding = binding if binding is not None else dict(source_digest="a" * 64, contract_digest="b" * 64,
                            registry_digest="c" * 64, task="openwiki")
        self.e = dict(profile=PROFILE, run_id=self.run, current_binding=self.binding,
                      public_rounds=[], judges={}, usage={})
        native_usage = dict(input_tokens=2, cached_input_tokens=1, output_tokens=1)
        log = self.write("native.jsonl", ('{"type":"thread.started","thread_id":"builder-thread"}\n'
            '{"type":"turn.started"}\n' + json.dumps(dict(type="turn.completed", usage=native_usage)) + '\n').encode())
        self.e["builder_native"] = self.receipt("native.json", builder_session_id="builder-thread",
            model="deepseek-flash", effort="max", state="terminal", thread_ids=["builder-thread"], native_log=log)
        previous = feedback = None
        for i in (1, 2):
            directory = root / f"s{i}"
            directory.mkdir()
            meta = dict(builder_session_id="builder-thread", submission_number=i,
                        revision_of_candidate_digest=previous, feedback_digest=feedback)
            (directory / "solution.patch").write_text(f"patch{i}\n")
            (directory / "edit_report.json").write_text(json.dumps({"feedback_response": "Addresses failing public invariant"}))
            (directory / "run_report.json").write_text(json.dumps(meta))
            digest = tree_digest(directory)
            result = self.receipt(f"result{i}.json", candidate_digest=digest, case="dev_001",
                                  state="terminal", classification="execution_valid")
            fb = self.write(f"feedback{i}.txt", f"full feedback round {i}\n".encode())
            fb["digest_algorithm"] = "sha256-bytes-v1"
            execution = dict(**meta, candidate_digest=digest, case="dev_001", state="terminal",
                classification="execution_valid", current_binding=self.binding, transport="complete", build_exit_code=0,
                materialized_source_digest_before_build=str(i) * 64, materialized_source_digest_after_build=str(i) * 64,
                materialized_repository_digest=str(i + 2) * 64,
                result=result, feedback_sha256=fb["sha256"])
            execution["request_ids"] = [f"public_lower-request-{i}"]
            if i == 2:
                execution["consumed_feedback"] = self.write("consumed.txt", (root / "feedback1.txt").read_bytes())
            row = dict(**meta, case="dev_001", candidate_digest=digest, submission_dir=f"s{i}",
                submission_sha256={n: sha256_file(directory / n) for n in FILES},
                materialized_source_digest=str(i) * 64, feedback=fb,
                materialized_repository_digest=str(i + 2) * 64,
                execution=self.receipt(f"execution{i}.json", **execution))
            self.e["public_rounds"].append(row)
            previous, feedback = digest, fb["sha256"]
        self.e["freeze"] = self.receipt("freeze.json", delivery_candidate_digest=previous, candidate_digest="4" * 64,
            builder_session_id="builder-thread", submission_sha256=row["submission_sha256"], current_binding=self.binding)
        freeze_sha = self.e["freeze"]["sha256"]
        hr = self.receipt("hidden-result.json", case="test_001", state="terminal", candidate_digest="4" * 64)
        self.e["hidden_smoke"] = self.receipt("hidden.json", case="test_001", state="terminal",
            classification="execution_valid", transport="complete", readiness_only=True, builder_access=False,
            freeze_sha256=freeze_sha, result=hr)
        self.mutate(self.e["hidden_smoke"], request_ids=["hidden_lower-request-1"])
        for role in ("result", "code"):
            inp = self.write(f"{role}-input.json", dict(role=role, freeze_sha256=freeze_sha))
            out = self.write(f"{role}-output.json", dict(role=role, judge_session_id=role,
                payload=dict(score=0, assessment="Functional failure, complete pipeline.")))
            self.e["judges"][role] = self.receipt(f"{role}-judge.json", role=role, judge_session_id=role,
                model="deepseek-flash", effort="max", state="terminal", formal=False, freeze_sha256=freeze_sha,
                request_id=role + "_judge-request-1", input=inp, output=out)
        for role in ROLES:
            if role == "builder":
                self.e["usage"][role] = self.receipt("builder-ledger.json", role=role,
                    transport="native_codex_direct", builder_broker_started=False, native_completed_turns=1,
                    native_reported_usage=[native_usage], actual_upstream_requests=None,
                    complete_provider_billing_claimed=False, native_usage_complete=True, known_tokens=3,
                    builder_session_id="builder-thread", native_log=log)
                continue
            count = 2 if role == "public_lower" else 1
            requests = []
            for i in range(1, count + 1):
                request = dict(request_id=f"{role}-request-{i}", state="success", usage_known=True, upstream_attempts=1, known_tokens=3)
                raw = self.write(f"{role}-raw-usage-{i}.json", request)
                requests.append(dict(**request, provider_record=raw))
            self.e["usage"][role] = self.receipt(f"{role}-ledger.json", role=role, calls=count, actual_upstream_attempts=count,
                successes=count, failures=0, known_tokens=3 * count, unknown_usage=0, in_flight=0, requests=requests)
        before = self.receipt("before.json", resources={"owned": dict(run_id=self.run, state="terminal"), "unrelated": {"state": "running"}})
        after = self.receipt("after.json", resources={"unrelated": {"state": "running"}})
        stats = self.receipt("stats.json", resource_id="owned", cpu_ns=0)
        self.e["cleanup"] = self.receipt("cleanup.json", before=before, after=after, owned_resources=["owned"],
            unit_state="terminal", harbor_state="terminal", builder_state="terminal", stats={"owned": stats})

    def write(self, name, value):
        path = self.root / name
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode())
        return dict(path=name, sha256=sha256_file(path))

    def receipt(self, name, **fields):
        return self.write(name, dict(run_id=self.run, owner="evaluator", **fields))

    def mutate(self, ref, **fields):
        value = json.loads((self.root / ref["path"]).read_text())
        value.update(fields)
        ref.update(self.write(ref["path"], value))

    def seal(self):
        return self.write("manifest.json", self.e)["sha256"]

    def validate(self):
        return validate_evidence(self.e, self.root, self.seal(), self.binding, judge_output_validators=VALIDATORS,
                                 broker_record_normalizers=NORMALIZERS)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = Bundle(Path(self.tmp.name).resolve())

    def test_complete_fixture(self):
        self.assertEqual(self.b.validate(), (True, []))

    def test_external_anchors_required(self):
        b = self.b
        for args in ((None, b.seal(), b.binding), (b.root, None, b.binding), (b.root, b.seal(), None)):
            self.assertFalse(validate_evidence(b.e, *args)[0])

    def test_evidence_cannot_differ_from_anchored_manifest(self):
        b = self.b
        anchor = b.seal()
        modified = copy.deepcopy(b.e)
        modified["run_id"] = "substituted"
        self.assertIn("differs", validate_evidence(modified, b.root, anchor, b.binding)[1][0])

    def test_cross_task_binding(self):
        b = self.b
        self.assertFalse(validate_evidence(b.e, b.root, b.seal(), dict(b.binding, task="codex"))[0])

    def test_actual_submission_mutation(self):
        (self.b.root / "s1/solution.patch").write_text("changed")
        self.assertFalse(self.b.validate()[0])

    def test_feedback_bytes_mutation(self):
        (self.b.root / "feedback1.txt").write_text("changed")
        self.assertFalse(self.b.validate()[0])

    def test_symlink_leaf_and_parent_rejected(self):
        b = self.b
        orig = b.root / "feedback1.txt"
        orig.rename(b.root / "original-feedback")
        orig.symlink_to("original-feedback")
        self.assertIn("symlink", b.validate()[1][0])
        orig.unlink(); (b.root / "original-feedback").rename(orig)
        (b.root / "alias").symlink_to("s1", target_is_directory=True)
        b.e["public_rounds"][0]["submission_dir"] = "alias"
        self.assertIn("symlink", b.validate()[1][0])

    def test_lexical_escape_rejected(self):
        self.b.e["public_rounds"][0]["submission_dir"] = "s2/../s1"
        self.assertIn("escape", self.b.validate()[1][0])

    def test_infrastructure_round_and_source_drift(self):
        ref = self.b.e["public_rounds"][0]["execution"]
        self.b.mutate(ref, classification="infrastructure_error")
        self.assertFalse(self.b.validate()[0])
        self.b.mutate(ref, classification="execution_valid", materialized_source_digest_after_build="f" * 64)
        self.assertFalse(self.b.validate()[0])

    def test_freeze_delivery_materialized_distinction(self):
        self.b.mutate(self.b.e["freeze"], candidate_digest=self.b.e["public_rounds"][-1]["candidate_digest"])
        self.assertFalse(self.b.validate()[0])

    def test_judge_must_be_independent(self):
        self.b.mutate(self.b.e["judges"]["result"], judge_session_id="builder-thread")
        self.assertFalse(self.b.validate()[0])

    def test_usage_missing_negative_bool_unknown_inflight_and_aggregate(self):
        b = self.b
        ref = b.e["usage"]["hidden_lower"]
        for change in (dict(unknown_usage=1), dict(in_flight=1), dict(failures=-1), dict(known_tokens=True),
                       dict(actual_upstream_attempts=0), dict(successes=0), dict(calls=0)):
            original = json.loads((b.root / ref["path"]).read_text())
            b.mutate(ref, **change)
            self.assertFalse(b.validate()[0], change)
            ref.update(b.write(ref["path"], original))

    def test_provider_record_bytes_required(self):
        (self.b.root / "hidden_lower-raw-usage-1.json").write_text("{}")
        self.assertFalse(self.b.validate()[0])

    def test_owned_cleanup_and_unrelated_protection(self):
        b = self.b
        cleanup = json.loads((b.root / "cleanup.json").read_text())
        b.mutate(cleanup["after"], resources={})
        b.mutate(b.e["cleanup"], after=cleanup["after"])
        self.assertIn("unrelated", b.validate()[1][0])

    def test_cleanup_live_rejected(self):
        self.b.mutate(self.b.e["cleanup"], harbor_state="running")
        self.assertFalse(self.b.validate()[0])

    def test_second_native_thread_rejected(self):
        b = self.b
        native = json.loads((b.root / "native.json").read_text())
        native["native_log"] = b.write("native.jsonl", b'{"type":"thread.started","thread_id":"builder-thread"}\n{"type":"thread.started","thread_id":"new"}\n')
        b.mutate(b.e["builder_native"], native_log=native["native_log"])
        self.assertFalse(b.validate()[0])

    def test_public_requests_cannot_be_reused(self):
        self.b.mutate(self.b.e["public_rounds"][1]["execution"], request_ids=["public_lower-request-1"])
        self.assertFalse(self.b.validate()[0])

    def test_report_only_revision_rejected(self):
        b = self.b
        second = b.e["public_rounds"][1]
        (b.root / "s2/solution.patch").write_bytes((b.root / "s1/solution.patch").read_bytes())
        second["submission_sha256"]["solution.patch"] = sha256_file(b.root / "s2/solution.patch")
        second["candidate_digest"] = tree_digest(b.root / "s2")
        execution = json.loads((b.root / "execution2.json").read_text())
        b.mutate(execution["result"], candidate_digest=second["candidate_digest"])
        b.mutate(second["execution"], candidate_digest=second["candidate_digest"], result=execution["result"])
        self.assertIn("no product change", b.validate()[1][0])

    def test_judge_schema_valid_flag_cannot_replace_validation(self):
        b = self.b
        judge = json.loads((b.root / "result-judge.json").read_text())
        judge["output"] = b.write("result-output.json", {"schema_valid": True})
        b.mutate(b.e["judges"]["result"], output=judge["output"])
        self.assertFalse(b.validate()[0])

    def test_judge_validators_external_required(self):
        b = self.b
        self.assertFalse(validate_evidence(b.e, b.root, b.seal(), b.binding)[0])

    def test_native_builder_cannot_invent_broker_requests(self):
        b = self.b
        b.mutate(b.e["usage"]["builder"], actual_upstream_requests=1, calls=1, requests=[{}])
        self.assertFalse(b.validate()[0])

    def test_native_builder_usage_missing_terminal_turn(self):
        b = self.b
        log = b.write("native.jsonl", b'{"type":"thread.started","thread_id":"builder-thread"}\n{"type":"turn.started"}\n')
        b.mutate(b.e["builder_native"], native_log=log)
        b.mutate(b.e["usage"]["builder"], native_log=log)
        self.assertFalse(b.validate()[0])

    def test_actual_mixed_harbor_log_preserves_raw_bytes(self):
        b = self.b
        raw = (b.root / "native.jsonl").read_bytes()
        mixed = b"stderr: native diagnostic\n{\n  \"rendered\": true\n}\n" + raw
        log = b.write("native.jsonl", mixed)
        b.mutate(b.e["builder_native"], native_log=log)
        b.mutate(b.e["usage"]["builder"], native_log=log)
        self.assertEqual(b.validate(), (True, []))

    def test_malformed_exact_native_frame_rejected(self):
        b = self.b
        raw = (b.root / "native.jsonl").read_bytes()
        log = b.write("native.jsonl", raw + b'{"type":broken}\n')
        b.mutate(b.e["builder_native"], native_log=log)
        b.mutate(b.e["usage"]["builder"], native_log=log)
        self.assertFalse(b.validate()[0])

    def test_freeze_cannot_substitute_input_source_digest(self):
        b = self.b
        b.mutate(b.e["freeze"], candidate_digest=b.e["public_rounds"][1]["materialized_source_digest"])
        self.assertFalse(b.validate()[0])

    def test_actual_feedback_canonical_payload_digest(self):
        import hashlib
        b = self.b
        payload = {"round": 1, "feedback": "Address public invariant"}
        digest = hashlib.sha256((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        feedback = b.write("feedback1.txt", dict(payload, feedback_digest=digest))
        feedback["digest_algorithm"] = "canonical-json-without-feedback-digest-v1"
        b.e["public_rounds"][0]["feedback"] = feedback
        b.mutate(b.e["public_rounds"][0]["execution"], feedback_sha256=feedback["sha256"])
        second = b.e["public_rounds"][1]
        report = json.loads((b.root / "s2/run_report.json").read_text())
        report["feedback_digest"] = digest
        (b.root / "s2/run_report.json").write_text(json.dumps(report))
        second["feedback_digest"] = digest
        second["submission_sha256"]["run_report.json"] = sha256_file(b.root / "s2/run_report.json")
        second["candidate_digest"] = tree_digest(b.root / "s2")
        execution = json.loads((b.root / "execution2.json").read_text())
        b.mutate(execution["result"], candidate_digest=second["candidate_digest"])
        consumed = b.write("consumed.txt", (b.root / "feedback1.txt").read_bytes())
        b.mutate(second["execution"], feedback_digest=digest, candidate_digest=second["candidate_digest"],
                 consumed_feedback=consumed, result=execution["result"])
        b.mutate(b.e["freeze"], delivery_candidate_digest=second["candidate_digest"], submission_sha256=second["submission_sha256"])
        freeze_sha = b.e["freeze"]["sha256"]
        b.mutate(b.e["hidden_smoke"], freeze_sha256=freeze_sha)
        for role in ("result", "code"):
            judge = json.loads((b.root / f"{role}-judge.json").read_text())
            b.mutate(judge["input"], freeze_sha256=freeze_sha)
            b.mutate(b.e["judges"][role], freeze_sha256=freeze_sha, input=judge["input"])
        self.assertEqual(b.validate(), (True, []))


if __name__ == "__main__":
    unittest.main()
