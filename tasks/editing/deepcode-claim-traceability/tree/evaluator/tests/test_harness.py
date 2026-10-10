from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from evaluator.harness.case_specs import CASE_SPECS, HIDDEN_CASES, PUBLIC_CASES, load_manifest
from evaluator.harness.common import SOURCE_REPOSITORY, SOURCE_TREE_SHA256, _validate_reports, patch_paths, path_is_allowed, stable_tree_digest
from evaluator.harness.controller import CandidateController, ProtocolError
from evaluator.harness.evaluate import DIMENSION_IDS, PUBLIC_WEIGHTS, _case_safety_violation, _checksum_ok, _graph_ok
from evaluator.harness.hidden_runner import load_freeze
from evaluator.harness.builder_lifecycle import BuilderSession
from evaluator.harness import run_hidden as hidden_suite_runner

ROOT = Path(__file__).resolve().parents[2]


class HarnessContractTests(unittest.TestCase):
    def test_case_inventory_is_exact(self):
        self.assertEqual(PUBLIC_CASES, ["dev_001", "dev_002"])
        self.assertEqual(HIDDEN_CASES, [f"test_{index:03d}" for index in range(1, 7)])
        self.assertEqual(set(CASE_SPECS), set(PUBLIC_CASES + HIDDEN_CASES))
        self.assertEqual(sorted(path.name for path in (ROOT / "dev_cases").glob("dev_*")), PUBLIC_CASES)
        self.assertEqual(sorted(path.name for path in (ROOT / "test_cases").glob("test_*")), HIDDEN_CASES)

    def test_manifests_total_100_and_dimensions(self):
        self.assertEqual(sum(PUBLIC_WEIGHTS.values()), 100)
        for case_id in HIDDEN_CASES:
            manifest = load_manifest(case_id)
            self.assertEqual(manifest["schema_version"], "1.0")
            self.assertEqual(manifest["case_id"], case_id)
            self.assertEqual(sum(manifest["assertions"].values()), 100)
            # Hidden cases deliberately use an axis-specific private rubric.
            # They may score only a subset of the shared assertion vocabulary,
            # but every selected assertion must be known and the selected
            # dimensions must still account for all 100 points.
            self.assertTrue(set(manifest["assertions"]).issubset(PUBLIC_WEIGHTS))
            self.assertTrue(manifest.get("excluded_axes"))
            assigned = set()
            for ids in DIMENSION_IDS.values():
                assigned.update(ids)
            self.assertTrue(set(manifest["assertions"]).issubset(assigned))

    def test_cases_have_no_oracles(self):
        forbidden = {"oracle.json", "rubric.md", "manifest.json", "expected.json", "score.json", "judge_context.md"}
        for case_id in PUBLIC_CASES + HIDDEN_CASES:
            parent = ROOT / ("dev_cases" if case_id.startswith("dev") else "test_cases") / case_id
            self.assertTrue((parent / "input.md").is_file())
            for path in parent.rglob("*"):
                if path.is_file():
                    self.assertNotIn(path.name, forbidden)
                    self.assertNotIn("assertion", path.name.lower())

    def test_policy_inputs_are_valid_and_scoped(self):
        for case_id in PUBLIC_CASES + HIDDEN_CASES:
            parent = ROOT / ("dev_cases" if case_id.startswith("dev") else "test_cases") / case_id
            policy = json.loads((parent / "assets" / "project" / ".deepcode" / "traceability_policy.json").read_text())
            self.assertEqual(policy["schema_version"], "1.0")
            self.assertIsInstance(policy["policy_version"], int)
            self.assertEqual(policy["required_review_roles"], ["scientist", "maintainer"])
            self.assertTrue(all(policy["roles"][role] for role in ("scientist", "maintainer", "operator", "auditor", "security")))

    def test_patch_policy_and_source_hash(self):
        self.assertEqual(len(SOURCE_TREE_SHA256), 64)
        self.assertEqual(stable_tree_digest(SOURCE_REPOSITORY), SOURCE_TREE_SHA256)
        self.assertEqual(patch_paths("diff --git a/core/x.py b/core/x.py\n"), ["core/x.py"])
        self.assertTrue(path_is_allowed("workflows/traceability_revisions.py"))
        self.assertFalse(path_is_allowed("evaluator/oracle.py"))
        self.assertFalse(path_is_allowed("../escape.py"))

    def test_checksum_and_graph_helpers_are_nonvacuous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "payload").mkdir()
            (root / "payload" / "input.txt").write_text("stable")
            checksums = {"schema_version": "1.0", "files": {"payload/input.txt": hashlib.sha256(b"stable").hexdigest()}}
            self.assertFalse(_checksum_ok(root, checksums), "a one-file map cannot earn checksum validity")
        spec = CASE_SPECS["dev_001"]
        bad = {"traceability_graph.json": {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "missing"}]}}
        self.assertFalse(_graph_ok(bad, spec))

    def test_rubric_totals_and_ceiling(self):
        text = (ROOT / "evaluator" / "rubric.md").read_text()
        weights = [
            int(match.group(1))
            for match in re.finditer(
                r"^## .+ - (\d+) points.*$",
                text,
                re.MULTILINE,
            )
        ]
        self.assertEqual(weights, [12, 8, 8, 12, 3, 57])
        self.assertTrue(
            "case-local ceiling of 35" in text or "case-local ceiling of 35" in text
        )

    def test_public_package_has_no_hidden_or_evaluator_references(self):
        builder_files = [ROOT / "README.md", *sorted((ROOT / "input").glob("*.md")), *sorted((ROOT / "dev_cases").rglob("*.md")), ROOT / "dev_cases" / "run_public.py"]
        combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in builder_files)
        self.assertNotIn("evaluator.harness", combined)
        self.assertNotIn("CASE_SPECS", combined)
        self.assertNotIn("ISOLATION_CANARY_", combined)
        hidden_tokens = [item.encode() for case_id in HIDDEN_CASES for item in CASE_SPECS[case_id]["ids"]]
        hidden_tokens += [b"codex-001-corrected", b"PRIVATE_test_"]
        for base in (ROOT / "input" / "repository", ROOT / "dev_cases"):
            for path in base.rglob("*"):
                if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                    payload = path.read_bytes()
                    for token in hidden_tokens:
                        self.assertNotIn(token, payload, f"builder-visible leak {token!r} in {path}")

    def test_delivery_schema_rejects_legacy_nested_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "edit_report.json").write_text(json.dumps({
                "schema_version": "1.0", "changed_paths": ["core/x.py"],
                "feature_summary": "x", "commands": [], "compatibility_notes": ["none"], "limitations": ["none"],
            }))
            (root / "run_report.json").write_text(json.dumps({
                "schema_version": "1.0", "status": "completed",
                "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"],
                "errors": [], "runtime": {"seconds": 1}, "peak_memory_bytes": 0,
                "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0},
            }))
            with self.assertRaises(Exception):
                _validate_reports(root, ["core/x.py"])

    def test_delivery_schema_requires_array_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "edit_report.json").write_text(json.dumps({
                "schema_version": "1.0", "changed_paths": ["core/x.py"],
                "feature_summary": "x", "commands": [], "compatibility_notes": "none", "limitations": ["none"],
            }))
            (root / "run_report.json").write_text(json.dumps({
                "schema_version": "1.0", "status": "completed",
                "artifact_paths": ["solution.patch", "edit_report.json", "run_report.json"],
                "errors": [], "runtime_seconds": 1.0, "peak_memory_bytes": 0,
                "api_calls": {"gateway": 0, "serper": 0, "web_retrieval": 0},
            }))
            with self.assertRaises(Exception):
                _validate_reports(root, ["core/x.py"])

    def test_case_directories_have_no_generated_state(self):
        forbidden = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv"}
        case_roots = [*(ROOT / "dev_cases").glob("dev_*"), *(ROOT / "test_cases").glob("test_*")]
        for parent in case_roots:
            for path in parent.rglob("*"):
                self.assertNotIn(path.name, forbidden)
                if path.is_file():
                    self.assertNotIn(path.suffix, {".pyc", ".sqlite", ".db"})

    def test_new_behavioral_branches_are_executed_by_canonical_harness(self):
        source = (ROOT / "evaluator" / "harness" / "evaluate.py").read_text(encoding="utf-8")
        self.assertIn("ThreadPoolExecutor", source)
        self.assertIn('"execution-reconcile"', source)
        self.assertIn("wrong-scope-plan", source)
        self.assertIn("stale-attestation-promote", source)

    def test_axis_specific_scope_cannot_be_gated_by_excluded_families(self):
        source = (ROOT / "evaluator" / "harness" / "evaluate.py").read_text(encoding="utf-8")
        self.assertIn('selected_any("QUARANTINE-RECOVERY", "QUARANTINE-SAFETY")', source)
        self.assertIn('selected_any("AUDIT-LEDGER", "AUDIT-AUTH-REPLAY")', source)
        self.assertNotIn('and bool(audit_ok)', source)
        self.assertNotIn('and bool(quarantine_ok)', source)
        self.assertFalse(_case_safety_violation({"CAPSULE-COMPAT"}, scope_ok=False, stale_fenced=False))
        self.assertTrue(_case_safety_violation({"SCOPE-NONDISCLOSURE"}, scope_ok=False, stale_fenced=True))
        self.assertTrue(_case_safety_violation({"EXEC-RECOVERY-FENCE"}, scope_ok=True, stale_fenced=False))

    def test_freeze_manifest_is_candidate_two_immutable_and_iso(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            base.mkdir()
            (base / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            (base / "cli").mkdir(); (base / "core").mkdir()
            c1, c2 = root / "candidate-1", root / "candidate-2"
            c1.mkdir(); c2.mkdir()
            (c1 / "deepcode.py").write_text("one\n", encoding="utf-8")
            (c2 / "deepcode.py").write_text("two\n", encoding="utf-8")
            (c1 / "cli").mkdir(); (c1 / "core").mkdir()
            (c2 / "cli").mkdir(); (c2 / "core").mkdir()
            controller = CandidateController(
                base_repository=base,
                run_dir=root / "run",
                launcher=ROOT / "evaluator/harness/deepcode_lower_agent.py",
                broker_endpoint=None,
                dry_run=True,
            )
            controller.submit(c1)
            controller.submit(c2)
            frozen = controller.freeze()
            self.assertEqual(frozen["source_submission"], 2)
            self.assertTrue(frozen["frozen_tree_read_only"])
            self.assertTrue(frozen["frozen_digest_stable"])
            parsed = datetime.fromisoformat(frozen["frozen_at"])
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(frozen["candidate_digest"], frozen["frozen_digest_before"])
            self.assertEqual(frozen["candidate_digest"], frozen["frozen_digest_after"])
            manifest, path, digest, _ = load_freeze(root / "run/freeze_manifest.json")
            self.assertEqual(manifest["source_submission"], 2)
            self.assertEqual(path, Path(frozen["candidate_path"]).resolve())
            self.assertEqual(digest, frozen["candidate_digest"])
            for item in [path, *path.rglob("*")]:
                if item.is_symlink():
                    continue
                self.assertEqual(item.stat().st_mode & 0o222, 0, item)

    def test_live_controller_duplicate_is_idempotent_non_consuming_and_freeze_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            base.mkdir()
            (base / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            (base / "cli").mkdir(); (base / "core").mkdir()
            candidates = []
            for number in range(1, 4):
                candidate = root / f"candidate-{number}"
                candidate.mkdir()
                (candidate / "deepcode.py").write_text(f"ROUND = {number}\n", encoding="utf-8")
                (candidate / "cli").mkdir(); (candidate / "core").mkdir()
                candidates.append(candidate)
            controller = CandidateController(
                base_repository=base,
                run_dir=root / "run",
                launcher=ROOT / "evaluator/harness/deepcode_lower_agent.py",
                broker_endpoint="http://127.0.0.1:1/v1/responses",
                public_case_ids=("dev_001",),
                max_dev_rounds=2,
            )

            def successful_build(repository: Path, output: Path):
                return {"exit_code": 0, "digest": repository.name}

            with (
                patch("evaluator.harness.controller.build", side_effect=successful_build) as build_mock,
                patch.object(
                    controller,
                    "_run_dev",
                    side_effect=lambda _repository, case_id, round_no: {
                        "case_id": case_id,
                        "round": round_no, "score": 75, "semantic_score_contract_valid": True,
                    },
                ) as dev_mock,
            ):
                first = controller.submit(candidates[0])
                second = controller.submit(candidates[1])
                duplicate = controller.submit(candidates[0])

            self.assertIs(duplicate, first)
            self.assertEqual(len(controller.records), 2)
            self.assertEqual(build_mock.call_count, 2)
            self.assertEqual(dev_mock.call_count, 2)
            self.assertFalse((root / "run/candidates/candidate_003").exists())
            self.assertEqual(
                sorted(path.name for path in (root / "run").glob("dev_feedback_candidate_*.json")),
                ["dev_feedback_candidate_001.json", "dev_feedback_candidate_002.json"],
            )

            frozen = controller.freeze()
            self.assertEqual(frozen["source_submission"], second["source_submission"])
            self.assertEqual(frozen["candidate_digest"], second["candidate_digest"])
            self.assertEqual(frozen["accepted_submission_count"], 2)
            self.assertEqual(frozen["dev_feedback"], [first, second])
            self.assertIs(controller.submit(candidates[1]), second)
            self.assertEqual(controller.frozen, frozen)
            with self.assertRaisesRegex(ProtocolError, "already frozen"):
                controller.submit(candidates[2])
            self.assertEqual(
                list((root / "run/candidates").glob(".candidate_attempt_*")),
                [],
            )

    def test_builder_session_duplicate_preserves_prior_feedback_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"; base.mkdir()
            (base / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            (base / "cli").mkdir(); (base / "core").mkdir()
            candidate = root / "candidate"; candidate.mkdir()
            (candidate / "deepcode.py").write_text("VALUE = 1\n", encoding="utf-8")
            (candidate / "cli").mkdir(); (candidate / "core").mkdir()
            second_candidate = root / "candidate-2"; second_candidate.mkdir()
            (second_candidate / "deepcode.py").write_text("VALUE = 2\n", encoding="utf-8")
            (second_candidate / "cli").mkdir(); (second_candidate / "core").mkdir()
            controller = CandidateController(
                base_repository=base,
                run_dir=root / "run",
                launcher=ROOT / "evaluator/harness/deepcode_lower_agent.py",
                broker_endpoint=None,
                dry_run=True,
                max_dev_rounds=2,
            )
            session = BuilderSession(
                controller,
                session_id="builder-session-idempotent-retry",
                require_feedback_ack=True,
            )

            accepted = session.submit(candidate)
            original_digest = session.feedback_digest
            original_feedback = session.feedback_path.read_bytes()
            duplicate = session.submit(candidate)

            self.assertIs(duplicate, accepted)
            self.assertTrue(session.last_submit_idempotent)
            self.assertEqual(len(controller.records), 1)
            self.assertEqual(session.feedback_digest, original_digest)
            self.assertEqual(session.feedback_path.read_bytes(), original_feedback)
            self.assertFalse(session.feedback_consumed)
            self.assertEqual(session.last_submit_feedback["feedback_digest"], original_digest)

            consumed = session.consume_feedback(original_digest)
            consumed_feedback = session.feedback_path.read_bytes()
            self.assertTrue(session.feedback_consumed)
            self.assertEqual(consumed_feedback, original_feedback, "feedback bytes remain immutable after consumption")
            self.assertIs(session.submit(candidate), accepted)
            self.assertTrue(session.last_submit_idempotent)
            self.assertTrue(session.feedback_consumed)
            self.assertEqual(session.feedback_digest, original_digest)
            self.assertEqual(session.feedback_path.read_bytes(), consumed_feedback)
            self.assertEqual(session.last_submit_feedback["feedback_digest"], original_digest)

            second = session.submit(
                second_candidate,
                feedback_digest_ack=original_digest,
            )
            second_feedback_digest = session.feedback_digest
            second_feedback = session.feedback_path.read_bytes()
            self.assertNotEqual(second_feedback_digest, original_digest)
            self.assertIs(session.submit(candidate), accepted)
            self.assertTrue(session.last_submit_idempotent)
            self.assertEqual(session.last_submit_feedback["feedback_digest"], original_digest)
            self.assertEqual(session.last_submit_feedback, json.loads(original_feedback))
            self.assertEqual(session.feedback_digest, second_feedback_digest)
            self.assertEqual(session.feedback_path.read_bytes(), second_feedback)
            frozen = session.freeze()
            self.assertEqual(frozen["source_submission"], second["source_submission"])
            self.assertEqual(frozen["accepted_submission_count"], 2)

    def test_hidden_executor_uses_only_canonical_case_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"; base.mkdir()
            (base / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            (base / "cli").mkdir(); (base / "core").mkdir()
            c1, c2 = root / "candidate-1", root / "candidate-2"
            c1.mkdir(); c2.mkdir()
            (c1 / "deepcode.py").write_text("one\n", encoding="utf-8")
            (c2 / "deepcode.py").write_text("two\n", encoding="utf-8")
            (c1 / "cli").mkdir(); (c1 / "core").mkdir()
            (c2 / "cli").mkdir(); (c2 / "core").mkdir()
            fake = root / "fake_launcher.py"
            fake.write_text(
                "import argparse, json, pathlib\n"
                "p=argparse.ArgumentParser(); p.add_argument('--output'); p.add_argument('--workspace'); p.add_argument('--case-id'); a,_=p.parse_known_args()\n"
                "out=pathlib.Path(a.output); ws=pathlib.Path(a.workspace); out.mkdir(parents=True, exist_ok=True); ws.mkdir(parents=True, exist_ok=True)\n"
                "stats={'schema_version':'deepcode-agentloop-broker-stats-v1','protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high','credential_owner':'evaluator-broker'},'runtime':{'calls':0,'failures':0,'successful_calls':0,'started_at':0},'calls':[],'failures':[],'tokens':{'input':0,'output':0,'total':0}}\n"
                "json.dump(stats, open(out/'broker_before.json','w')); stats['runtime']={'calls':1,'failures':0,'successful_calls':1,'started_at':0}; json.dump(stats, open(out/'broker_after.json','w'))\n"
                "json.dump({'schema_version':'deepcode-agentloop-result/v1','case_id':a.case_id,'observations':[]}, open(ws/'agent_result.json','w'))\n"
                "json.dump({'schema_version':'deepcode-agentloop-case-result-v1','case_id':a.case_id,'contract_valid':True,'classification':'candidate_valid','broker_delta':{'calls':1,'failures':0,'successful_calls':1},'model_protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high'},'credential_isolation':{'candidate_credential':'placeholder-only','real_credential_exposed':False},'runtime':{'sandbox':{'mechanism':'test'}}}, open(out/'result.json','w'))\n",
                encoding="utf-8",
            )
            controller = CandidateController(
                base_repository=base,
                run_dir=root / "run",
                launcher=fake,
                broker_endpoint="http://127.0.0.1:1/v1/responses",
                hidden_root=ROOT / "test_cases",
                dry_run=True,
            )
            controller.submit(c1); controller.submit(c2); controller.freeze()
            result = controller.run_hidden("test_001")
            output = root / "run/hidden/test_001"
            self.assertEqual(result["case_id"], "test_001")
            self.assertEqual(result["classification"], "candidate_valid")
            self.assertTrue(result["hidden_started_after_freeze"])
            self.assertTrue(result["frozen_digest_stable"])
            self.assertFalse(result["candidate_visibility"]["canonical_test_case_tree"])
            for name in ("launcher.json", "broker_evidence.json", "isolation_evidence.json", "terminal_artifact_evidence.json", "case-attestation.json", "result.json"):
                self.assertTrue((output / name).is_file(), name)

    def test_hidden_executor_rejects_non_iso_or_non_candidate_two_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate"; candidate.mkdir()
            (candidate / "file").write_text("x", encoding="utf-8")
            manifest = {
                "schema_version": "deepcode-agentloop-freeze-v1",
                "candidate_digest": "0" * 64,
                "candidate_path": str(candidate),
                "frozen_at": "2026-09-03T00:00:00+00:00",
                "dev_feedback": [], "hidden_allowed": True, "source_submission": 1,
                "frozen_tree_read_only": True, "frozen_digest_before": "0" * 64,
                "frozen_digest_after": "0" * 64, "frozen_digest_stable": True,
            }
            path = root / "freeze.json"; path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_freeze(path)

    def test_builder_session_requires_feedback_before_candidate_two(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"; base.mkdir()
            (base / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            (base / "cli").mkdir(); (base / "core").mkdir()
            c1, c2 = root / "candidate-1", root / "candidate-2"
            c1.mkdir(); c2.mkdir()
            (c1 / "deepcode.py").write_text("one\n", encoding="utf-8")
            (c2 / "deepcode.py").write_text("two\n", encoding="utf-8")
            (c1 / "cli").mkdir(); (c1 / "core").mkdir()
            (c2 / "cli").mkdir(); (c2 / "core").mkdir()
            controller = CandidateController(
                base_repository=base,
                run_dir=root / "run",
                launcher=ROOT / "evaluator/harness/deepcode_lower_agent.py",
                broker_endpoint=None,
                dry_run=True,
            )
            session = BuilderSession(
                controller,
                session_id="builder-session-test",
                require_feedback_ack=True,
            )
            session.submit(c1)
            with self.assertRaises(ProtocolError):
                session.submit(c2)
            feedback = session.consume_feedback()
            self.assertTrue(session.feedback_consumed)
            with self.assertRaises(ProtocolError):
                session.submit(c2, feedback_digest_ack="0" * 64)
            session.submit(c2, feedback_digest_ack=feedback["feedback_digest"])
            frozen = session.freeze()
            self.assertEqual(frozen["source_submission"], 2)
            attestation = json.loads((root / "run/builder-session-attestation.json").read_text())
            self.assertTrue(attestation["same_session"])
            self.assertTrue(attestation["candidate_1_and_2"])
            self.assertTrue(attestation["distinct_digests"])
            self.assertTrue(attestation["feedback_consumed"])
            self.assertTrue(attestation["feedback_ack_matches"])

    def test_ten_round_feedback_and_hidden_attestations_bind_latest_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"; base.mkdir()
            (base / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            (base / "cli").mkdir(); (base / "core").mkdir()
            fake = root / "fake_launcher.py"
            fake.write_text(
                "import argparse, json, pathlib\n"
                "p=argparse.ArgumentParser(); p.add_argument('--output'); p.add_argument('--workspace'); p.add_argument('--case-id'); a,_=p.parse_known_args()\n"
                "out=pathlib.Path(a.output); ws=pathlib.Path(a.workspace); out.mkdir(parents=True, exist_ok=True); ws.mkdir(parents=True, exist_ok=True)\n"
                "stats={'schema_version':'deepcode-agentloop-broker-stats-v1','protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high','credential_owner':'evaluator-broker'},'runtime':{'calls':0,'failures':0,'successful_calls':0,'started_at':0},'calls':[],'failures':[],'tokens':{'input':0,'output':0,'total':0}}\n"
                "json.dump(stats, open(out/'broker_before.json','w')); stats['runtime']={'calls':1,'failures':0,'successful_calls':1,'started_at':0}; json.dump(stats, open(out/'broker_after.json','w'))\n"
                "json.dump({'schema_version':'deepcode-agentloop-result/v1','case_id':a.case_id,'observations':[]}, open(ws/'agent_result.json','w'))\n"
                "json.dump({'schema_version':'deepcode-agentloop-case-result-v1','case_id':a.case_id,'contract_valid':True,'classification':'candidate_valid','broker_delta':{'calls':1,'failures':0,'successful_calls':1},'model_protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high'},'credential_isolation':{'candidate_credential':'placeholder-only','real_credential_exposed':False},'runtime':{'sandbox':{'mechanism':'test'}}}, open(out/'result.json','w'))\n",
                encoding="utf-8",
            )
            controller = CandidateController(
                base_repository=base,
                run_dir=root / "run",
                launcher=fake,
                broker_endpoint="http://127.0.0.1:1/v1/responses",
                hidden_root=ROOT / "test_cases",
                dry_run=True,
                max_dev_rounds=10,
            )
            session = BuilderSession(
                controller,
                session_id="builder-session-ten-rounds",
                require_feedback_ack=True,
            )
            for round_no in range(1, 11):
                candidate = root / f"candidate-{round_no}"
                candidate.mkdir()
                (candidate / "deepcode.py").write_text(f"ROUND = {round_no}\n", encoding="utf-8")
                (candidate / "cli").mkdir(); (candidate / "core").mkdir()
                if round_no == 1:
                    session.submit(candidate)
                else:
                    feedback = session.consume_feedback()
                    session.submit(candidate, feedback_digest_ack=feedback["feedback_digest"])
            frozen = session.freeze()
            self.assertEqual(frozen["source_submission"], 10)
            self.assertEqual(frozen["accepted_submission_count"], 10)
            builder_attestation = json.loads(session.attestation_path.read_text(encoding="utf-8"))
            self.assertTrue(builder_attestation["feedback_chain_complete"])
            self.assertEqual(len(builder_attestation["feedback_acknowledgements"]), 9)
            self.assertTrue(all(item["matches"] for item in builder_attestation["feedback_acknowledgements"]))

            case_result = controller.run_hidden("test_001")
            self.assertEqual(case_result["source_submission"], 10)
            case_attestation = json.loads(
                (root / "run/hidden/test_001/case-attestation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(case_attestation["source_submission"], 10)

            suite_output = root / "suite"
            synthetic_result = {
                "schema_version": "deepcode-agentloop-hidden-run-v2",
                "case_id": "test_001",
                "hidden_started_after_freeze": True,
                "frozen_digest_stable": True,
                "evidence": {"result": str(root / "unused-result.json")},
            }
            with patch.object(hidden_suite_runner, "run", return_value=synthetic_result):
                exit_code = hidden_suite_runner.main([
                    "--freeze-manifest", str(root / "run/freeze_manifest.json"),
                    "--hidden-root", str(ROOT / "test_cases"),
                    "--output", str(suite_output),
                    "--broker-endpoint", "http://127.0.0.1:1/v1/responses",
                    "--case", "test_001",
                ])
            self.assertEqual(exit_code, 0)
            suite_attestation = json.loads(
                (suite_output / "hidden-after-freeze-attestation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(suite_attestation["source_submission"], 10)


if __name__ == "__main__":
    unittest.main()
