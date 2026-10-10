"""Provider-free execution attribution and acceptance boundary regressions."""
import json
import io
import http.server
import threading
import urllib.request
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentloop.evaluator import lower_agent_launcher as lower
from agentloop.evaluator.controller import Controller
from agentloop.evaluator.hidden_attestation import attest
from agentloop.evaluator.semantic_score import execution_verdict, SHARED
from agentloop.evaluator.case_contract import validate_visible_case
from agentloop.protocol import candidate_tree_digest
from evaluator import formal_finalize as finalizer
from harbor import acceptance_reuse
from agentloop.evaluator import broker as lower_broker
from agentloop.evaluator import product_lifecycle


class LifecycleTests(unittest.TestCase):
    def test_product_resource_and_owner_contract(self):
        with mock.patch.dict('os.environ', {'AGENTSWE_CLAUDE_PRODUCT_OWNER': 'claude-' + 'a' * 32}):
            command = lower.product_command(Path('/candidate'), Path('/workspace'), ['python3', '-V'])
        self.assertEqual(command[command.index('--memory') + 1], '4g')
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertIn('agentswe.claude.case-owner=claude-' + 'a' * 32, command)

    def test_owned_cleanup_rejects_unproven_container(self):
        owner, cid = 'claude-' + 'a' * 32, 'b' * 64
        responses = [subprocess.CompletedProcess([], 0, cid + '\n', ''),
                     subprocess.CompletedProcess([], 0, json.dumps({product_lifecycle.OWNER_LABEL: 'different-owner'}), '')]
        with mock.patch.object(product_lifecycle.subprocess, 'run', side_effect=responses) as invoked:
            with self.assertRaisesRegex(OSError, 'ownership changed'):
                product_lifecycle.cleanup_owned(owner)
        self.assertEqual(invoked.call_count, 2)

    def test_staged_acceptance_reuses_exact_frozen_inputs_and_rejects_case_or_execution_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'run'
            frozen = run / 'lifecycle/frozen_candidate'
            frozen.mkdir(parents=True)
            (frozen / 'product.py').write_text('pass\n')
            delivery = run / 'delivery'
            delivery.mkdir()
            (delivery / 'solution.patch').write_text('fixture patch')
            requirements = run / 'formal_scoring/code_public_requirements'
            requirements.mkdir(parents=True)
            (requirements / '01.md').write_text('public only')
            visible, oracle = root / 'test.json', root / 'oracle.json'
            visible.write_text('visible fixture')
            oracle.write_text('private fixture')
            digest = candidate_tree_digest(frozen)
            records = {
                'acceptance_source.json': {'candidate_digest': digest,
                    'delivery_digest': acceptance_reuse.delivery_digest(delivery)},
                'lifecycle/freeze_manifest.json': {'candidate_digest': digest,
                    'hidden_case_inventory': ['test_001']},
                'code_preflight.json': {'preflight_valid': True, 'lower_started': False,
                    'code_full_tree_digest': 'full-tree'},
                'formal_scoring/code_source_binding.json': {'fixture': True},
            }
            for name, data in records.items():
                (run / name).write_text(json.dumps(data))
            args = SimpleNamespace(run_dir=run, delivery=delivery, cases=['test_001'], hidden_cases_dir=root)
            with mock.patch.object(acceptance_reuse, 'prepare_code_inputs', return_value=(requirements, 'full-tree')), \
                 mock.patch.object(acceptance_reuse, 'load_case_bundle', return_value=({}, {}, visible, oracle)):
                identity = acceptance_reuse.staged_identity(args)
                (run / 'staged_acceptance_identity.json').write_text(json.dumps(identity))
                self.assertEqual(acceptance_reuse.resume_preflight(args), (frozen.resolve(), digest))
                oracle.write_text('different private world')
                with self.assertRaisesRegex(ValueError, 'inputs changed'):
                    acceptance_reuse.resume_preflight(args)
                oracle.write_text('private fixture')
                (run / 'hidden.cid').write_text('prior attempted launch')
                with self.assertRaisesRegex(ValueError, 'already attempted'):
                    acceptance_reuse.resume_preflight(args)

    def test_acceptance_parameter_flow_reaches_real_broker_forced_gateway_medium(self):
        """Exercise run() -> startup args -> real HTTP handler -> upstream request."""
        servers = []
        captured = []
        original_urlopen = urllib.request.urlopen
        class Response(io.BytesIO):
            status = 200
        def transport(request, *args, **kwargs):
            if request.full_url.startswith('http://127.0.0.1:'):
                return original_urlopen(request, *args, **kwargs)
            captured.append({'url': request.full_url, 'body': json.loads(request.data),
                             'authorization': request.get_header('Authorization')})
            return Response(json.dumps({'object': 'response', 'status': 'completed', 'output_text': 'provider-free fixture',
                'usage': {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10}}).encode())
        def controlled_opener(*, on_request_start=None, **_kwargs):
            def open_request(request, *args, **kwargs):
                if on_request_start is not None: on_request_start()
                return transport(request, *args, **kwargs)
            return SimpleNamespace(open=open_request)
        def startup(**kwargs):
            if kwargs['script'] != acceptance_reuse.ROOT / 'agentloop/evaluator/broker.py':
                return
            server = http.server.ThreadingHTTPServer(('127.0.0.1', kwargs['port']), lower_broker.Handler)
            server.state = lower_broker.BrokerState()
            # Preserve the broker's actual default if the caller forgets the route.
            server.provider_url = kwargs.get('provider_url', 'https://api.openai.com/v1/responses')
            server.provider_key = 'evaluator-only-fixture-key'
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        def hidden(**kwargs):
            result = lower.model_call(kwargs['broker_endpoint'], 'broker-only-placeholder', 'fixture prompt')
            self.assertEqual(result['output_text'], 'provider-free fixture')
            stats = lower.broker_stats(kwargs['broker_endpoint'])
            self.assertEqual(stats['protocol'], {'model': 'gpt-5.6-sol', 'reasoning_effort': 'high'})
            self.assertEqual(stats['runtime']['successful_calls'], 1)
            self.assertEqual(stats['credential']['candidate_visible'], 'broker-only-placeholder')
        def materialize(_baseline, _patch, output):
            output.mkdir(parents=True)
            (output / 'fixture.py').write_text('pass\n')
            return {'build_valid': True}
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                delivery = root / 'delivery'
                delivery.mkdir()
                (delivery / 'solution.patch').write_text('provider-free transport fixture')
                args = SimpleNamespace(run_dir=root / 'run', delivery=delivery, cases=['test_001'],
                    hidden_cases_dir=root / 'cases', stage_only=False, credential_file=root / 'not-a-real-key')
                with mock.patch.object(acceptance_reuse, 'validate_delivery', return_value=[]), \
                     mock.patch.object(acceptance_reuse, 'load_case_bundle'), \
                     mock.patch.object(acceptance_reuse, 'materialize', side_effect=materialize), \
                     mock.patch.object(acceptance_reuse, 'preflight_code') as code_preflight, \
                     mock.patch.object(acceptance_reuse, 'start_broker', side_effect=startup), \
                     mock.patch.object(acceptance_reuse, 'run_hidden', side_effect=hidden), \
                     mock.patch.object(acceptance_reuse, 'attest'), \
                     mock.patch.object(acceptance_reuse, 'finalize', return_value=(2, {'mode': 'provider_free_test'})), \
                     mock.patch.object(acceptance_reuse, 'cleanup_owned_container', return_value={'absent_after_cleanup': True}), \
                     mock.patch.object(lower_broker, 'direct_opener', side_effect=controlled_opener), \
                     mock.patch.object(urllib.request, 'urlopen', side_effect=transport):
                    self.assertEqual(acceptance_reuse.run(args), 2)
                    code_preflight.assert_called_once()
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]['url'], 'https://gateway.example.com/v1/responses')
            self.assertEqual(captured[0]['body']['model'], 'gpt-5.6-sol')
            self.assertEqual(captured[0]['body']['reasoning']['effort'], 'high')
            self.assertEqual(captured[0]['authorization'], 'Bearer evaluator-only-fixture-key')
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()

    def test_acceptance_lower_uses_gateway_instead_of_legacy_default(self):
        with mock.patch.object(acceptance_reuse, 'start_broker') as broker:
            acceptance_reuse.start_acceptance_lower(name='owned', credential=Path('/evaluator-only'),
                                                    port=18001, cidfile=Path('/new-run/hidden.cid'))
            self.assertEqual(broker.call_args.kwargs['provider_url'], 'https://gateway.example.com/v1/responses')
            self.assertEqual(broker.call_args.kwargs['script'], acceptance_reuse.ROOT / 'agentloop/evaluator/broker.py')

    def test_code_identity_binds_full_tree_separately_from_native_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.py').write_text('pass\n')
            native = candidate_tree_digest(root)
            code = finalizer.code_tree_digest(root)
            self.assertNotEqual(native, code)
            (root / '.agentloop_build.json').write_text('{"valid": true}')
            self.assertEqual(native, candidate_tree_digest(root))
            self.assertNotEqual(code, finalizer.code_tree_digest(root))
            code = finalizer.code_tree_digest(root)
            (root / 'source.py').write_text('raise RuntimeError()\n')
            self.assertNotEqual(native, candidate_tree_digest(root))
            self.assertNotEqual(code, finalizer.code_tree_digest(root))

    def test_code_requirements_are_all_public_documents_not_duplicate_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'task/input'
            source.mkdir(parents=True)
            for name in ('01_task_goal.md', '02_interface_and_delivery.md', '03_requirements_and_constraints.md', '04_resources.md'):
                (source / name).write_text(name)
            (source / 'repository').mkdir()
            (source / 'repository/old.py').write_text('baseline')
            (root / 'task/agentloop').mkdir()
            (root / 'task/agentloop/protocol.py').write_text('test-only digest implementation evidence')
            frozen = root / 'candidate'
            frozen.mkdir()
            (frozen / 'edited.py').write_text('edited source')
            with mock.patch.object(finalizer, 'ROOT', root / 'task'):
                req, code = finalizer.prepare_code_inputs(root / 'run', frozen, candidate_tree_digest(frozen))
                self.assertEqual(len(list(req.iterdir())), 4)
                self.assertFalse((req / 'repository').exists())
                self.assertEqual(code, finalizer.code_tree_digest(frozen))
                (source / '01_task_goal.md').write_text('changed after freeze')
                with self.assertRaisesRegex(ValueError, 'requirements changed'):
                    finalizer.prepare_code_inputs(root / 'run', frozen, candidate_tree_digest(frozen))

    def launch_missing_product(self, probe_exit):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugin"
            plugin.mkdir()
            case = root / "dev.json"
            case.write_text(json.dumps({"schema_version": "agentswe-claude-public-case/v1",
                "case_id": "dev_001", "task": "Observe the product policy decision.",
                "policy": {"schema_version": 2, "mode": "enforce", "default_decision": "deny"},
                "allowed_actions": [{"id": "audit", "type": "inspector_command", "argv": ["--audit"]}]}))
            argv = ["lower", "--plugin-root", str(plugin), "--case", str(case),
                "--workspace", str(root / "workspace"), "--output", str(root / "output"),
                "--candidate-digest", "d" * 64, "--broker-endpoint", "http://fixture/v1/responses"]
            stats = {"runtime": {"calls": 0, "failures": 0, "successful_calls": 0}}
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(lower, "broker_stats", return_value=stats), \
                 mock.patch.object(lower, "model_call") as model, \
                 mock.patch.object(lower.subprocess, "run", return_value=subprocess.CompletedProcess([], probe_exit, "", "fixture")):
                self.assertEqual(lower.main(), 2)
                model.assert_not_called()
            result = json.loads((root / "output/trajectory.json").read_text())
            self.assertEqual(result["candidate_digest"], "d" * 64)
            self.assertTrue(Path(result["failure_attribution"]["evidence_paths"][0]).is_file())
            if (SHARED / "execution_contract.py").is_file():
                verdict, zero = execution_verdict(result, "dev_001", "d" * 64)
                self.assertEqual(verdict["classification"], "candidate_zero" if probe_exit == 0 else "infrastructure_invalid")
                self.assertEqual(zero is not None, probe_exit == 0)
            return result

    def test_missing_candidate_entry_after_healthy_runtime_is_observed_candidate_failure(self):
        result = self.launch_missing_product(0)
        self.assertEqual(result["classification"], "candidate_product_failure")
        self.assertTrue(result["environment_preflight"]["valid"])
        self.assertEqual(result["failure_attribution"]["party"], "candidate")
        self.assertTrue(result["failure_attribution"]["fatal"])
        self.assertFalse(result["infrastructure_invalid"])

    def test_runtime_failure_precedes_missing_candidate_entry(self):
        result = self.launch_missing_product(125)
        self.assertEqual(result["classification"], "evaluator_infrastructure_failure")
        self.assertFalse(result["environment_preflight"]["valid"])
        self.assertFalse(result["failure_attribution"]["fatal"])
        self.assertTrue(result["infrastructure_invalid"])

    def test_public_round_requires_authoritative_semantic_or_observed_zero(self):
        self.assertFalse(Controller._dev_is_freeze_eligible({"score": 100, "classification": "candidate_valid"}))
        self.assertFalse(Controller._dev_is_freeze_eligible({"score": 0, "score_kind": "candidate_zero", "infrastructure_invalid": True}))
        self.assertTrue(Controller._dev_is_freeze_eligible({"score": 0, "score_kind": "candidate_zero"}))
        self.assertTrue(Controller._dev_is_freeze_eligible({"score": 73, "score_kind": "independent_result_rubric"}))

    def test_reduced_attestation_needs_explicit_acceptance(self):
        with self.assertRaisesRegex(ValueError, "acceptance"):
            attest(run_dir=Path("/unused"), case_ids=["test_001"])
        with self.assertRaisesRegex(ValueError, "acceptance"):
            attest(run_dir=Path("/unused"), case_ids=["test_001", "test_001"], acceptance=True)

    def test_fixture_cannot_manufacture_success_post_event(self):
        case = {"case_id": "dev_001", "task": "Observe initial state.",
                "allowed_actions": [{"id": "audit", "type": "inspector_command", "argv": ["--audit"]}],
                "initial_events": [{"hook_event_name": "PostToolUse", "tool_use_id": "prior", "tool_input": {}}]}
        with self.assertRaisesRegex(ValueError, "never recovery or completion"):
            validate_visible_case(case, hidden=False)


if __name__ == "__main__":
    unittest.main()
