"""Zero API controls: real submit/controller/scorer, local case-runner fixture."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'harbor'), '@@AGENTSWE_EDITING_CONTROL@@']
from evaluator.harness import controller
from evaluator.harness.builder_lifecycle import BuilderSession
from evaluator.harness.candidate_adapter import tree_digest
from evaluator.harness.public_execution_diagnostic import collect_public_diagnostic
from evaluator.harness.public_feedback import public_record
from harbor.formal_one_stop import SocketLifecycle


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.case = self.root / 'task/dev_cases/dev_002'
        self.case.mkdir(parents=True)
        (self.case / 'input.md').write_text('Synthetic public case; no model invocation.')
        self.run = self.root / 'lifecycle'
        self.evaluation = self.run / 'evaluations/candidate_001_attempt_1'
        self.repository = self.run / 'candidates/candidate_001'
        self.repository.mkdir(parents=True)
        (self.repository / 'product.py').write_text('synthetic fixture')
        self.output = self.evaluation / 'dev_002'
        self.output.mkdir(parents=True)
        self.resource = self.evaluation / 'dev_002-case-resources'
        fixture = os.environ.get('DEEPCODE_DIAGNOSTIC_RESOURCE_FIXTURE')
        if fixture:
            self.resource.mkdir()
            for name in ('resource-attestation.json', 'scope-ownership.json', 'scope-ready.json'):
                shutil.copyfile(Path(fixture) / name, self.resource / name)
        else:
            token = 'a' * 32
            unit = 'agentswe-edit-owner-c-' + token + '.scope'
            ownership = dict(unit=unit, description='AgentSWE case owner ' + token,
                cgroup='/system.slice/' + unit, memory_bytes=4294967296)
            observed = dict(pid=123, cgroup=ownership['cgroup'], memory_max='4294967296', memory_swap_max='0')
            att = dict(schema_version='agentswe-owned-case-resources/v1', **ownership,
                valid=True, timed_out=True, configuration_delta=[], timeout_seconds=600,
                independent_scope_deadline_seconds=590, cleanup_reserve_seconds=10,
                elapsed_seconds=590.117, observed=observed,
                systemd_properties=dict(Id=unit, Description=ownership['description'], ControlGroup=ownership['cgroup']),
                cleanup=dict(complete=True, unit_state='inactive', cgroup_events='populated 0\n', identity_verified_before_stop=True))
            for name, value in [('resource-attestation.json', att), ('scope-ownership.json', ownership), ('scope-ready.json', observed)]:
                put(self.resource / name, value)
        self.request = self.evaluation / 'dev_002-case-request.json'
        self.write_request()

    def write_request(self):
        put(self.request, dict(repository=str(self.repository), case=str(self.case), output=str(self.output),
                              case_id='dev_002', candidate_digest=tree_digest(self.repository), deadline=1514648.981200862))

    def collect(self):
        return collect_public_diagnostic(run_dir=self.run, evaluation_root=self.evaluation,
            repository=self.repository, case_root=self.case, case_id='dev_002', candidate_digest=tree_digest(self.repository))

    def test_valid_terminal_facts_and_missing_phase(self):
        value = self.collect().public_dict()
        self.assertEqual(value['case_id'], 'dev_002')
        d = value['public_execution_diagnostic']
        self.assertEqual((d['case_total_budget_seconds'], d['work_deadline_seconds'], d['cleanup_reserve_seconds']), (600, 590, 10))
        self.assertIs(d['scope_timed_out'], True)
        self.assertEqual(d['elapsed_seconds'], 590.117)
        self.assertIsNone(d['last_completed_phase'])
        self.assertFalse(d['phase_timing_available'])
        self.assertFalse(d['final_artifact_present'])
        put(self.output / 'workspace/agent_result.json', {'invalid': 'present is not valid'})
        self.assertTrue(self.collect().final_artifact_present)

    def test_missing_evidence_no_diagnostic(self):
        for name in ('resource-attestation.json', 'scope-ownership.json', 'scope-ready.json'):
            with self.subTest(name=name):
                path = self.resource / name; data = path.read_bytes(); path.unlink()
                self.assertIsNone(self.collect()); path.write_bytes(data)

    def test_strict_types_ranges_and_scope_identity(self):
        path = self.resource / 'resource-attestation.json'; original = json.loads(path.read_text())
        changes = [('timed_out', 'true'), ('timed_out', 1), ('elapsed_seconds', True),
            ('elapsed_seconds', float('nan')), ('elapsed_seconds', float('inf')),
            ('elapsed_seconds', -1), ('elapsed_seconds', 606), ('elapsed_seconds', '590'),
            ('timeout_seconds', True), ('timeout_seconds', 601), ('cleanup_reserve_seconds', 9),
            ('independent_scope_deadline_seconds', 589), ('valid', 1),
            ('unit', 'agentswe-edit-owner-c-' + 'b' * 32 + '.scope'), ('configuration_delta', ['x'])]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(original); changed[key] = value; put(path, changed)
                self.assertIsNone(self.collect())
        put(path, original)
        for owner_name in ('scope-ownership.json', 'scope-ready.json'):
            path = self.resource / owner_name; original = path.read_bytes(); put(path, {'forged': True})
            self.assertIsNone(self.collect()); path.write_bytes(original)

    def test_request_binding_and_candidate_cannot_select_case(self):
        original = json.loads(self.request.read_text())
        for key, value in [('case_id', 'dev_001'), ('output', str(self.output / '../dev_001')),
                           ('repository', str(self.root / 'other')), ('case', str(self.root / 'private')),
                           ('deadline', True), ('candidate_digest', '0' * 64)]:
            changed = dict(original); changed[key] = value; put(self.request, changed)
            self.assertIsNone(self.collect())
        put(self.request, original)
        self.assertIsNone(collect_public_diagnostic(run_dir=self.run, evaluation_root=self.evaluation,
            repository=self.repository, case_root=self.case, case_id='test_001', candidate_digest=tree_digest(self.repository)))

    def test_symlink_escape_cycle_and_hardlink_refused(self):
        path = self.resource / 'resource-attestation.json'; content = path.read_bytes()
        outside = self.root / 'outside.json'; outside.write_bytes(content)
        path.unlink(); path.symlink_to(outside); self.assertIsNone(self.collect())
        path.unlink(); path.symlink_to(path); self.assertIsNone(self.collect())
        path.unlink(); os.link(outside, path); self.assertIsNone(self.collect())
        path.unlink(); path.write_bytes(content)
        (self.output / 'workspace').symlink_to(self.root); self.assertIsNone(self.collect())

    def test_private_fields_are_never_projected(self):
        path = self.resource / 'resource-attestation.json'; value = json.loads(path.read_text())
        value.update(auth='SECRET_CANARY', hidden='/home/private/case', raw={'token': 'SECRET_CANARY'}, last_completed_phase='invented')
        put(path, value)
        public = json.dumps(self.collect().public_dict())
        for text in ('SECRET_CANARY', '/home/', 'hidden', 'raw', 'invented', 'cgroup', 'pid'):
            self.assertNotIn(text, public)

    def make_owner(self, transform=None):
        # Only case-runner process is replaced by a terminal fixture producer.
        # Actual controller, lifecycle, infra scorer and formal.submit run below.
        shutil.rmtree(self.run)
        ctrl = controller.CandidateController(base_repository=self.root / 'task/input/repository',
            run_dir=self.run, launcher=self.root / 'unused.py', broker_endpoint='http://unused.invalid',
            public_case_ids=('dev_002',))
        owner = SocketLifecycle.__new__(SocketLifecycle)
        owner.lock = threading.RLock(); owner.controller = ctrl
        owner.session = BuilderSession(ctrl, session_id='synthetic-control')
        owner.session_id = 'synthetic-control'; owner.public_case_ids = ('dev_002',)
        owner.workspace = self.root / 'delivery'; owner.workspace.mkdir(exist_ok=True)
        (owner.workspace / 'product.py').write_text('synthetic fixture')
        owner.accepted_deliveries = {}; owner.events = []
        owner.bind_native_thread = lambda: None
        owner.validate = lambda reason: (200, {'ready_for_submission': True})
        owner.event = lambda name, **kw: owner.events.append({'event': name, **kw})
        self.calls = []
        # Save synthetic or real original receipts before replacing the local run.
        receipts = self.saved_receipts
        def terminal_case(command, **kwargs):
            self.assertEqual(Path(command[2]).name, 'dev_case_runner.py')
            self.assertEqual(command[1], '-I'); self.assertEqual(command[3], '--request')
            request_path = Path(command[4]); request = json.loads(request_path.read_text())
            request['deadline'] = 1514648.981200862; put(request_path, request)
            out = Path(request['output']); out.mkdir(parents=True)
            resource = out.with_name(out.name + '-case-resources')
            resource.mkdir()
            for name, raw in receipts.items(): (resource / name).write_bytes(raw)
            if transform: transform(out, resource, request_path)
            self.calls.append(command)
            return subprocess.CompletedProcess(command, -15, '', '')
        self.addCleanup(patch.stopall)
        patch.object(controller, 'materialize', side_effect=lambda src, dst, **kw: shutil.copytree(src, dst)).start()
        patch.object(controller, 'build', return_value={'exit_code': 0, 'stdout': '', 'stderr': ''}).start()
        patch.object(controller.subprocess, 'run', side_effect=terminal_case).start()
        return owner

    def test_actual_submit_fallback_typed_422_and_no_replay(self):
        self.saved_receipts = {p.name: p.read_bytes() for p in self.resource.iterdir()}
        owner = self.make_owner()
        code, value = owner.submit(None)
        self.assertEqual(code, 422); self.assertFalse(value['submission_consumed']); self.assertFalse(value['retry_same_candidate'])
        self.assertEqual(value['public_execution_diagnostics'][0]['case_id'], 'dev_002')
        record = json.loads(next((self.run / 'rejected_evaluations').glob('*/record.json')).read_text())
        result = record['dev'][0]
        self.assertEqual(result['case_id'], 'dev_002')
        self.assertEqual(result['classification'], 'evaluator_infrastructure_error')
        self.assertIsNone(result['score']); self.assertFalse(result['semantic_score_contract_valid'])
        self.assertEqual(owner.controller.records, [])
        again, rejected = owner.submit(None)
        self.assertEqual(again, 422); self.assertIn('no replay', rejected['error'])
        self.assertNotIn('public_execution_diagnostics', rejected); self.assertEqual(len(self.calls), 1)

    def test_actual_missing_receipt_keeps_original_rejection(self):
        self.saved_receipts = {p.name: p.read_bytes() for p in self.resource.iterdir()}
        owner = self.make_owner(lambda out, resource, req: (resource / 'resource-attestation.json').unlink())
        code, value = owner.submit(None)
        self.assertEqual(code, 422); self.assertNotIn('public_execution_diagnostics', value)
        self.assertFalse(value['submission_consumed']); self.assertFalse(value['retry_same_candidate'])
        self.assertEqual(len(self.calls), 1)

    def test_actual_invalid_and_private_receipts_are_bounded(self):
        self.saved_receipts = {p.name: p.read_bytes() for p in self.resource.iterdir()}
        def corrupt(out, resource, req):
            p = resource / 'resource-attestation.json'; v = json.loads(p.read_text())
            v['elapsed_seconds'] = True; put(p, v)
        def escape(out, resource, req):
            p = resource / 'resource-attestation.json'; raw = p.read_bytes(); p.unlink()
            outside = self.root / 'external.json'; outside.write_bytes(raw); p.symlink_to(outside)
        def inject(out, resource, req):
            p = resource / 'resource-attestation.json'; v = json.loads(p.read_text())
            v.update(auth='PRIVATE_CANARY', raw='/data/private', hidden={'secret': 'PRIVATE_CANARY'})
            put(p, v)
        for change, expected in ((corrupt, False), (escape, False), (inject, True)):
            with self.subTest(change=change.__name__):
                patch.stopall()
                owner = self.make_owner(change)
                code, value = owner.submit(None)
                self.assertEqual(code, 422)
                self.assertEqual('public_execution_diagnostics' in value, expected)
                self.assertFalse(value['submission_consumed']); self.assertFalse(value['retry_same_candidate'])
                self.assertNotIn('PRIVATE_CANARY', json.dumps(value)); self.assertNotIn('/data/private', json.dumps(value))
                self.assertEqual(len(self.calls), 1)

    def test_actual_normal_accepted_feedback_unchanged(self):
        self.saved_receipts = {p.name: p.read_bytes() for p in self.resource.iterdir()}
        owner = self.make_owner()
        normal = {'case_id': 'dev_002', 'classification': 'candidate_valid', 'infra_valid': True,
            'score': 85, 'semantic_score_contract_valid': True, 'broker_delta': {'failures': 0},
            'semantic_judgement': {'feedback': {'assessment': 'synthetic public'}}}
        with patch.object(owner.controller, '_run_dev', return_value=normal):
            code, value = owner.submit(None)
        self.assertEqual(code, 200); self.assertEqual(len(owner.controller.records), 1)
        self.assertEqual(value['state'], 'feedback_ready'); self.assertTrue(value['submission_consumed'])
        self.assertNotIn('public_execution_diagnostic', json.dumps(value))
        self.assertEqual(owner.controller.records[0]['dev'][0], normal)

    def test_normal_public_score_projection_unchanged(self):
        record = {'round': 1, 'candidate_digest': 'abc', 'accepted': True, 'round_consumed': True,
            'build': {'exit_code': 0, 'stdout': '', 'stderr': ''}, 'dev': [{'case_id': 'dev_001',
            'classification': 'candidate_valid', 'infra_valid': True, 'score': 85,
            'semantic_score_contract_valid': True, 'semantic_judgement': {'feedback': {'assessment': 'public'}},
            'public_execution_diagnostic': {'raw': 'MUST_NOT_LEAK'}}]}
        expected = copy.deepcopy(record)
        expected['dev'][0].pop('public_execution_diagnostic'); expected['dev'][0].pop('semantic_judgement')
        expected['dev'][0]['feedback'] = {'assessment': 'public'}
        self.assertEqual(public_record(record), expected)


if __name__ == '__main__':
    unittest.main()
