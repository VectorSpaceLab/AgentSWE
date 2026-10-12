from __future__ import annotations
import hashlib, json, subprocess, sys, tempfile, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluator"))
sys.path.insert(0, str(ROOT / "agentloop"))
from semantic_finalize import artifact_for, is_infrastructure, validate_result_contract, _ai_scientist_trajectory_errors
from lower_agent_launcher import authoring_failure_is_infrastructure
from harbor import formal_one_stop
from semantic_finalize import _canonical_digest
from two_round_controller import _all_started_after_freeze
from agentloop.case_world import CASE_WORLD_SEQUENCES
from evaluator.tests.fixture_model_response import capture_artifact

class OfflineContractTests(unittest.TestCase):
    def test_result_judge_starts_after_hidden_and_not_in_pilot(self):
        source = (ROOT / "harbor" / "formal_one_stop.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("hidden = lifecycle.controller.run_hidden(hidden_case_ids)"),
            source.index("start_broker(judge_name"),
        )
        self.assertIn("if not pilot:", source[source.index("hidden = lifecycle.controller.run_hidden(hidden_case_ids)"):])

    def test_cleanup_requires_container_absence(self):
        calls = []
        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(argv, 1, "", "Error: No such object: current-container-id")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with tempfile.TemporaryDirectory() as td:
            cidfile = Path(td) / "owned.cid"
            cidfile.write_text("current-container-id\n", encoding="ascii")
            with patch.object(formal_one_stop.subprocess, "run", side_effect=fake_run):
                result = formal_one_stop.remove_and_verify_container("ai-scientist-owned-test", cidfile, attempted=True)
        self.assertTrue(result["absent_after_cleanup"])
        self.assertIn(["docker", "rm", "-f", "current-container-id"], calls)
        self.assertIn(["docker", "inspect", "current-container-id"], calls)
    def test_candidate_and_provider_classification(self):
        self.assertFalse(is_infrastructure({"classification":"candidate_no_model_call","classification_axis":"candidate"}))
        self.assertTrue(is_infrastructure({"classification":"mount_infrastructure_error","classification_axis":"infrastructure"}))
        self.assertTrue(authoring_failure_is_infrastructure(urllib.error.HTTPError("http://broker", 502, "bad gateway", {}, None)))
        self.assertFalse(authoring_failure_is_infrastructure(ValueError("lower model final result is not strict JSON")))
    def test_evaluator_synthesized_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            run=Path(td); base=run/"lifecycle/hidden/test_001"; base.mkdir(parents=True); (base/"agent_result.json").write_text(json.dumps({"case_id":"test_001","evaluator_synthesized":True}))
            with self.assertRaises(ValueError): artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",{})
    def test_model_artifact_uses_launcher_owned_provenance(self):
        self._assert_model_artifact_provenance(complete=True)

    def test_partial_case_world_remains_scoreable_with_intact_origin(self):
        self._assert_model_artifact_provenance(complete=False)

    def _assert_model_artifact_provenance(self, *, complete):
        # Synthetic origin-contract fixture only, not an executed Agent or a
        # benchmark reference solution. Both full and partial worlds must
        # retain exactly the same provenance rejection boundaries.
        with tempfile.TemporaryDirectory() as td:
            run=Path(td); benchmark=run/"benchmark"; base=run/"lifecycle/hidden/test_001"; base.mkdir(parents=True)
            task=benchmark/"agentloop/cases/test_001/task.md"; task.parent.mkdir(parents=True)
            task.write_text("canonical lower task\n", encoding="utf-8")
            task_digest=hashlib.sha256(task.read_bytes()).hexdigest()
            release=base/"verification_report.json"; release.write_text('{"ok":true}\n')
            release_digest=hashlib.sha256(release.read_bytes()).hexdigest()
            runtime_digest="a"*64; rollout_digest="b"*64
            (base/"case_runtime.json").write_text(json.dumps({"runtime_digest":runtime_digest,"runtime_projection":{"case_id":"test_001","evaluator_nonce":"fresh-nonce"},"task_source":"agentloop/cases/test_001/task.md","task_sha256":task_digest}))
            events=[{
                "sequence":index,"operation":operation,"exit_code":0,
                "observation_digest":chr(96 + index) * 64,
                "observation":{"receipt_digests":{},"release_artifact_hashes":{"verification_report.json":release_digest}},
                "evaluator_events":[{"sequence":index,"transition":transition,"advanced":True}],
            } for index, (operation, transition) in enumerate(zip(("verify", "verify"), CASE_WORLD_SEQUENCES["test_001"]), 1) if complete or index == 1]
            attempts = []
            for event in events:
                index = event["sequence"]
                evidence = base / "case_world_evidence" / f"attempt_{index:03d}.json"
                evidence.parent.mkdir(exist_ok=True)
                evidence.write_text(json.dumps({
                    "schema_version":"agentswe-ai-scientist-case-world-evidence-v2",
                    "case_id":"test_001", "attempt_sequence":index,
                    "action_sequence":index, "transition":event["evaluator_events"][0]["transition"],
                    "operation":event["operation"], "advanced":True,
                    "synthetic_unit_fixture":True,
                }))
                attempts.append({
                    **event["evaluator_events"][0], "attempt_sequence":index,
                    "action_sequence":index, "triggered_by_model_operation":event["operation"],
                    "evaluator_owned":True, "checks":{"synthetic_fixture":True},
                    "evidence_path":str(evidence.relative_to(base)),
                    "evidence_sha256":hashlib.sha256(evidence.read_bytes()).hexdigest(),
                })
                event["evaluator_events"] = [attempts[-1]]
            trajectory_digest=_canonical_digest(events)
            trajectory={
                "schema_version":"agentswe-ai-scientist-raw-action-trajectory-v1",
                "case_id":"test_001",
                "source":"evaluator-observed-model-selected-product-actions",
                "model":"gpt-5.6-sol","reasoning_effort":"high","max_action_steps":10,
                "events":events,"event_count":len(events),"trajectory_digest":trajectory_digest,
                "finish_decision":{"kind":"finish"},"action_error":None,
            }
            trajectory_path=base/"raw_action_trajectory.json"
            trajectory_path.write_text(json.dumps(trajectory),encoding="utf-8")
            artifact=base/"agent_result.json"
            artifact.write_text(json.dumps({
                "schema_version":"agent-result-v1","case_id":"test_001",
                "tool_events":[{"sequence":event["sequence"],"operation":event["operation"],"exit_code":event["exit_code"],"observation_digest":event["observation_digest"],"receipt_digests":{}} for event in events],
                "observed_facts":{
                    "trajectory_digest":trajectory_digest,"runtime_case_digest":runtime_digest,
                    "case_world_digest":"0"*64,
                    "operations":[event["operation"] for event in events],"receipt_digests_by_step":[{"sequence":event["sequence"],"digests":{}} for event in events],
                    "final_release_artifact_hashes":{"verification_report.json":release_digest},
                },
                "receipt_id":"receipt","state":"completed","rollout_digest":rollout_digest,
                "artifact_path":"verification_report.json","artifact_digest":release_digest,
                "honest_recovery":True,"safe":True,"oracle_leak":False,
            }))
            record={
                "case_id":"test_001","real_execution":True,"evaluator_synthesized":False,
                "task_source":"agentloop/cases/test_001/task.md","task_sha256":task_digest,
                "agent_artifact_source":"lower_model_final_response","authoring_call_completed":True,
                "agent_artifact_path":str(artifact),"agent_artifact_sha256":hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "model_protocol":{"model":"gpt-5.6-sol","reasoning_effort":"high"},
                "action_loop":{
                    "raw_trajectory_path":str(trajectory_path),"event_count":len(events),"operations":[event["operation"] for event in events],
                    "raw_trajectory_sha256":hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
                },
                "integrity":{"runtime_case_digest":runtime_digest,"rollout_digest":rollout_digest,
                    "trajectory_digest":trajectory_digest,"case_world_digest":"0"*64,"artifact_hashes":{"verification_report.json":release_digest}},
            }
            world = {
                "schema_version":"agentswe-ai-scientist-case-world-v2","case_id":"test_001",
                "sequence":list(CASE_WORLD_SEQUENCES["test_001"]),
                "events":attempts,"attempts":attempts,
                "event_count":len(events),"attempt_count":len(attempts),"complete":complete,
                "initial_history":{},
            }
            world["world_digest"] = _canonical_digest(world)
            (base / "case_world.json").write_text(json.dumps(world), encoding="utf-8")
            artifact_data = json.loads((base / "agent_result.json").read_text(encoding="utf-8"))
            artifact_data["observed_facts"]["case_world_digest"] = world["world_digest"]
            (base / "agent_result.json").write_text(json.dumps(artifact_data), encoding="utf-8")
            record["integrity"]["case_world_digest"] = world["world_digest"]
            record["agent_artifact_sha256"] = hashlib.sha256((base / "agent_result.json").read_bytes()).hexdigest()
            response = base / "model_final_response.txt"
            _, record["authoring_response_sha256"] = capture_artifact(artifact_data, response)
            self.assertEqual(artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark),artifact.resolve())
            self.assertEqual(json.loads((base / "case_world.json").read_text())["complete"], complete)
            # Missing or changed captured text is rejected, even when a
            # parseable final artifact and plausible launcher labels exist.
            captured = response.read_bytes()
            response.rename(base / "saved_response.txt")
            with self.assertRaisesRegex(ValueError,"captured final model response"):
                artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark)
            response.write_bytes(captured + b" ")
            with self.assertRaisesRegex(ValueError,"captured final model response"):
                artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark)
            response.write_text(json.dumps({**artifact_data,"state":"different-model-claim"}))
            record["authoring_response_sha256"] = hashlib.sha256(response.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,"differs from completed model response"):
                artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark)
            response.write_bytes(captured)
            record["authoring_response_sha256"] = hashlib.sha256(captured).hexdigest()
            evidence_path = base / attempts[0]["evidence_path"]
            original_evidence = evidence_path.read_bytes()
            evidence_path.write_bytes(original_evidence + b" ")
            with self.assertRaisesRegex(ValueError,"private case-world comparison evidence mismatch"):
                artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark)
            evidence_path.write_bytes(original_evidence)
            self.assertEqual(artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark),artifact.resolve())
            task.write_text("tampered lower task\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"task source/digest"):
                artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark)
            record["agent_artifact_sha256"]="0"*64
            task.write_text("canonical lower task\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"captured agent artifact digest"):
                artifact_for("ai_scientist",run,"test_001",base/"launcher_result.json",record,root=benchmark)
    def test_result_contract_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"c.json"; p.write_text(json.dumps({"contract_valid":False,"result_score_publishable":True,"case_id":"test_001","result_score":100,"judge":{"model":"gpt-5.6-sol","reasoning_effort":"max"},"provider_usage":{"logical_requests":1,"completed_responses":1}}))
            self.assertIsNotNone(validate_result_contract(p,"test_001")[1])
    def test_all_hidden_runtime_tasks_exist_and_cli_rejects_concurrency(self):
        for i in range(1,7): self.assertTrue((ROOT/f"agentloop/cases/test_{i:03d}/case_input.json").is_file())
        c=subprocess.run([sys.executable,str(ROOT/"harbor/formal_one_stop.py"),"--n-concurrent","3","--self-test"],text=True,capture_output=True); self.assertEqual(c.returncode,2)

    def test_hidden_attestation_verifies_each_start_after_freeze(self):
        freeze = "2026-09-07T00:00:10+00:00"
        after = [{"started_at": "2026-09-07T00:00:11+00:00"}]
        before = [{"started_at": "2026-09-07T00:00:09+00:00"}]
        self.assertTrue(_all_started_after_freeze(freeze, "2026-09-07T00:00:10+00:00", after))
        self.assertFalse(_all_started_after_freeze(freeze, "2026-09-07T00:00:10+00:00", before))
        self.assertFalse(_all_started_after_freeze("not-a-time", "2026-09-07T00:00:11+00:00", after))

    def test_legacy_case_world_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); base = run / "lifecycle/hidden/test_001"; base.mkdir(parents=True)
            events = [{
                "sequence": 1,
                "operation": "verify",
                "exit_code": 0,
                "observation_digest": "a" * 64,
                "observation": {"receipt_digests": {}},
                "evaluator_events": [{"sequence": 1, "transition": "case_started"}],
            }]
            trajectory = {
                "schema_version": "agentswe-ai-scientist-raw-action-trajectory-v1",
                "case_id": "test_001",
                "source": "evaluator-observed-model-selected-product-actions",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "events": events,
                "event_count": 1,
                "trajectory_digest": _canonical_digest(events),
            }
            trajectory_path = base / "raw_action_trajectory.json"
            trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
            world = {
                "schema_version": "agentswe-ai-scientist-case-world-v1",
                "case_id": "test_001",
                "sequence": ["case_started", "response_lost", "response_reconciled"],
                "events": [{"sequence": 1, "transition": "case_started"}],
                "event_count": 1,
                "complete": False,
            }
            world["world_digest"] = _canonical_digest({key: world[key] for key in ("schema_version", "case_id", "sequence", "events", "event_count", "complete")})
            (base / "case_world.json").write_text(json.dumps(world), encoding="utf-8")
            record = {
                "action_loop": {
                    "raw_trajectory_path": str(trajectory_path),
                    "event_count": 1,
                    "operations": ["verify"],
                    "raw_trajectory_sha256": hashlib.sha256(trajectory_path.read_bytes()).hexdigest(),
                },
                "integrity": {"case_world_digest": world["world_digest"], "trajectory_digest": trajectory["trajectory_digest"]},
            }
            errors = _ai_scientist_trajectory_errors(
                run, "test_001", {}, record, {}, base
            )
            self.assertIn("AI Scientist case-world identity mismatch", errors)

    def test_complete_hidden_inventory_reaches_independent_judges_even_if_not_publishable(self):
        cases=[f"test_{i:03d}" for i in range(1,7)]
        attestation={
            "evidence_kind":"formal","expected_cases":cases,"executed_cases":cases,
            "complete_inventory":True,"all_cases_materialized":True,"all_cases_real":True,
            "all_cases_started_after_freeze":True,"frozen_digest_stable":True,
            "formal_complete":False,
        }
        self.assertTrue(formal_one_stop.hidden_evidence_ready_for_finalizer(attestation))
        attestation["all_cases_real"]=False
        self.assertFalse(formal_one_stop.hidden_evidence_ready_for_finalizer(attestation))
if __name__ == "__main__": unittest.main()
