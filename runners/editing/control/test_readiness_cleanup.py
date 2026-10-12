import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import readiness_cleanup as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name).resolve()/'fresh'
        (self.run/'jobs/job/trial').mkdir(parents=True)
        (self.run/'jobs/job/result.json').write_text(json.dumps({'finished_at': 'now'}))
        (self.run/'jobs/job/trial/result.json').write_text(json.dumps({
            'finished_at': 'now', 'agent_execution': {'finished_at': 'now'}}))
        self.cid, self.nid, self.foreign = 'a'*64, 'b'*64, 'c'*64
        self.values = {
            self.cid: ('container', {'Id': self.cid, 'Name': 'owned',
                'State': {'Running': True, 'Status': 'running'}, 'Mounts': [],
                'Config': {'Labels': {'com.docker.compose.project': 'owned-project',
                    'com.docker.compose.project.working_dir': str(self.run/'builder_task/environment')}}}),
            self.nid: ('network', {'Id': self.nid, 'Name': 'owned-net',
                'Labels': {'com.docker.compose.project': 'owned-project'}, 'Containers': {self.cid: {}}}),
            self.foreign: ('container', {'Id': self.foreign, 'Name': 'unrelated',
                'Config': {'Labels': {}}, 'Mounts': [], 'State': {'Running': True, 'Status': 'running'}})}
        self.declaration = self.run/'resources.json'
        self.declaration.write_text(json.dumps({'owner': 'evaluator', 'run_id': self.run.name,
            'containers': [self.cid], 'networks': [self.nid]}))
        self.commands = []

    def command(self, argv):
        self.commands.append(argv)
        if argv[1] == 'stop':
            self.values[argv[-1]][1]['State'] = {'Running': False, 'Status': 'exited'}
        if 'rm' in argv:
            self.values.pop(argv[-1])
            if argv[-1] == self.cid:
                self.values[self.nid][1]['Containers'] = {}
        return '{}\n'

    def execute(self):
        with patch.object(cleanup, 'require_terminal_unit', return_value={'MainPID': '0'}), \
                patch.object(cleanup, 'inventory', side_effect=lambda: copy.deepcopy(self.values)), \
                patch.object(cleanup, 'inspect_json', side_effect=lambda kind, cid: copy.deepcopy(self.values.get(cid, (None, None))[1])), \
                patch.object(cleanup, 'command', side_effect=self.command):
            return cleanup.finalize(self.run, 'test.service', self.declaration, self.run/'cleanup')

    def test_owned_terminal_cleanup_preserves_foreign_resources(self):
        reference = self.execute()
        value = json.loads(Path(reference['path']).read_text())
        self.assertEqual(set(value['stats']), {self.cid, self.nid})
        before = json.loads(Path(value['before']['path']).read_text())
        after = json.loads(Path(value['after']['path']).read_text())
        self.assertEqual(before['resources'][self.cid]['state'], 'terminal')
        # A foreign resource is outside this run's projection now. Tracking the
        # whole machine meant an unrelated workload starting or stopping inside
        # the cleanup window wedged the run permanently against its own pinned
        # before.json. It is still never touched -- which is what the two
        # assertions below, not the projection, are there to prove.
        self.assertEqual(set(after['resources']), set())
        self.assertNotIn(self.foreign, before['resources'])
        self.assertTrue(self.values[self.foreign][1]['State']['Running'])
        self.assertTrue(all(cmd[-1] != self.foreign for cmd in self.commands))

    def test_live_harbor_refuses_before_any_docker_action(self):
        (self.run/'jobs/job/result.json').write_text('{"finished_at": null}')
        with self.assertRaisesRegex(ValueError, 'terminal evidence'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_missing_declared_container_is_not_cleanup_success(self):
        self.values.pop(self.cid)
        with self.assertRaisesRegex(ValueError, 'declared containers'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_network_with_foreign_member_refuses_mutation(self):
        self.values[self.nid][1]['Containers'][self.foreign] = {}
        with self.assertRaisesRegex(ValueError, 'unrelated attached'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_prefix_collision_is_not_run_ownership(self):
        self.values[self.cid][1]['Config']['Labels']['com.docker.compose.project.working_dir'] = str(self.run)+'-other'
        with self.assertRaisesRegex(ValueError, 'declared containers'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_unknown_unit_refuses_before_any_docker_action(self):
        with patch.object(cleanup, 'require_terminal_unit', side_effect=RuntimeError('not terminal')), \
                patch.object(cleanup, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'not terminal'):
                cleanup.finalize(self.run, 'bad.service', self.declaration, self.run/'cleanup')
        command.assert_not_called()

    def test_completed_cleanup_is_reused_without_stats_stop_or_remove(self):
        reference = self.execute()
        self.commands.clear()
        self.assertEqual(self.execute(), reference)
        self.assertEqual(self.commands, [])

    def test_failed_setup_can_cleanup_without_claiming_builder_execution(self):
        trial = self.run/'jobs/job/trial/result.json'
        trial.write_text(json.dumps({'finished_at': 'now', 'agent_execution': None,
            'exception_info': {'exception_type': 'HealthcheckError'}}))
        value = json.loads(Path(self.execute()['path']).read_text())
        self.assertEqual(value['builder_state'], 'not_started')
        self.assertEqual(value['unit_state'], 'terminal')

    def test_missing_builder_execution_without_exception_refuses_cleanup(self):
        (self.run/'jobs/job/trial/result.json').write_text(json.dumps({
            'finished_at': 'now', 'agent_execution': None}))
        with self.assertRaisesRegex(ValueError, 'terminal evidence'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_resume_after_remove_succeeded_but_client_raised_uses_original_intent(self):
        normal = self.command
        failed = [False]
        def interrupted(argv):
            value = normal(argv)
            if 'rm' in argv and argv[-1] == self.cid and not failed[0]:
                failed[0] = True
                raise subprocess.TimeoutExpired(argv, 30)
            return value
        self.command = interrupted
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute()
        root = self.run/'cleanup'
        plan = (root/'deletion_plan.json').read_bytes()
        saved_stats = (root/(self.cid+'.json')).read_bytes()
        self.assertTrue((root/(self.cid+'.delete-intent.json')).is_file())
        self.assertFalse((root/(self.cid+'.deleted.json')).exists())
        self.command = normal
        self.commands.clear()
        self.execute()
        self.assertEqual((root/'deletion_plan.json').read_bytes(), plan)
        self.assertEqual((root/(self.cid+'.json')).read_bytes(), saved_stats)
        self.assertFalse(any(argv[-1] == self.cid for argv in self.commands))
        self.assertTrue((root/(self.cid+'.deleted.json')).is_file())

    def test_absence_without_existing_deletion_intent_is_not_reconstructed(self):
        normal = self.command
        def interrupted(argv):
            if 'rm' in argv:
                raise RuntimeError('before actual remove')
            return normal(argv)
        self.command = interrupted
        with self.assertRaisesRegex(RuntimeError, 'actual remove'):
            self.execute()
        (self.run/'cleanup'/(self.cid+'.delete-intent.json')).unlink()
        self.values.pop(self.cid)
        self.values[self.nid][1]['Containers'] = {}
        self.command = normal
        self.commands.clear()
        with self.assertRaisesRegex(ValueError, 'without prior deletion intent'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_resume_preserves_stats_after_transient_stop_failure(self):
        normal = self.command
        def interrupted(argv):
            if argv[1] == 'stop':
                raise subprocess.TimeoutExpired(argv, 30)
            return normal(argv)
        self.command = interrupted
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute()
        path = self.run/'cleanup'/(self.cid+'.json')
        original = path.read_bytes()
        self.command = normal
        self.commands.clear()
        self.execute()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(any(argv[1] == 'stats' for argv in self.commands))

    def test_completed_cleanup_rejects_tampered_stats(self):
        self.execute()
        (self.run/'cleanup'/(self.cid+'.json')).write_text('{"resource_id":"tampered"}')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            self.execute()

    def test_completed_cleanup_rejects_changed_declaration(self):
        self.execute()
        value = json.loads(self.declaration.read_text())
        value['networks'] = []
        self.declaration.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.execute()

    def test_completed_cleanup_rechecks_actual_absence(self):
        original = copy.deepcopy(self.values[self.cid])
        self.execute()
        self.values[self.cid] = original
        with self.assertRaisesRegex(ValueError, 'no longer absent'):
            self.execute()

    def test_new_unrelated_resource_after_completed_cleanup_does_not_invalidate_original_receipt(self):
        reference = self.execute()
        self.values['d'*64] = copy.deepcopy(self.values[self.foreign])
        self.values['d'*64][1]['Id'] = 'd'*64
        self.assertEqual(self.execute(), reference)

    def test_plan_tampering_after_partial_remove_is_rejected(self):
        normal = self.command
        def interrupted(argv):
            value = normal(argv)
            if 'rm' in argv and argv[-1] == self.cid:
                raise RuntimeError('after delete')
            return value
        self.command = interrupted
        with self.assertRaisesRegex(RuntimeError, 'after delete'):
            self.execute()
        path = self.run/'cleanup/deletion_plan.json'
        plan = json.loads(path.read_text())
        plan['builder_state'] = 'not_started'
        path.write_text(json.dumps(plan))
        self.command = normal
        self.commands.clear()
        with self.assertRaisesRegex(ValueError, 'terminal proof changed'):
            self.execute()
        self.assertEqual(self.commands, [])

    def test_completed_cleanup_refuses_reused_unit_name_with_new_execution(self):
        self.execute()
        with patch.object(cleanup, 'require_terminal_unit', return_value={
                'MainPID': '0', 'ExecMainPID': 'replacement'}):
            with self.assertRaisesRegex(ValueError, 'execution identity changed'):
                cleanup.verify_completed_cleanup(self.run, 'test.service', self.declaration, self.run/'cleanup')

    def test_deletion_intent_is_durable_before_remove_and_terminal_plan_exists(self):
        normal = self.command
        def checked(argv):
            if 'rm' in argv:
                cid = argv[-1]
                root = self.run/'cleanup'
                intent = json.loads((root/(cid+'.delete-intent.json')).read_text())
                self.assertEqual(intent['deletion_plan'], cleanup.ref(root/'deletion_plan.json'))
                plan = json.loads((root/'deletion_plan.json').read_text())
                terminal = cleanup.checked_ref(plan['terminal_resources'], self.run)
                if terminal[cid]['kind'] == 'container':
                    self.assertEqual(terminal[cid]['detail']['State']['Status'], 'exited')
            return normal(argv)
        self.command = checked
        self.execute()


if __name__ == '__main__':
    unittest.main()
