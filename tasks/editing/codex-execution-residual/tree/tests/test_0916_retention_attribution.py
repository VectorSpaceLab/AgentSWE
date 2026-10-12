"""A container Harbor already removed is a terminal, attributable outcome.

Provider-free, no Docker. Harbor's DockerEnvironment.stop() runs `compose down`
unless keep_containers is set, and that flag has no configuration or CLI path in
this Harbor build, so deferred removal can never retain the Builder container.
Every readiness run therefore returned exit 125 and no freeze was ever possible.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harbor import builder_resources, readiness_resources

OWNED = 'a' * 64
BROKER = 'b' * 64


class AbsentIsTerminalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.observer = builder_resources.BuilderResourceObserver(self.run)
        self.observer.proof = {'observations': [{'container_id': OWNED,
                                                 'container_name': '/builder_task__x__env-main-1',
                                                 'image_id': 'sha256:deadbeef',
                                                 'compose_project': 'builder_task__x__env'}]}
        real = subprocess.run

        def fake(argv, *a, **kw):
            if argv[:3] == ['docker', 'inspect', OWNED]:
                return subprocess.CompletedProcess(argv, 1, '', 'Error: No such object: ' + OWNED)
            if argv[:2] == ['docker', 'ps']:
                return subprocess.CompletedProcess(argv, 0, '', '')
            raise AssertionError('unexpected command: ' + ' '.join(argv))

        def fake_check_output(argv, *a, **kw):
            return b''

        builder_resources.subprocess.run = fake
        self.real_check_output = builder_resources.subprocess.check_output
        builder_resources.subprocess.check_output = fake_check_output
        self.addCleanup(lambda: setattr(builder_resources.subprocess, 'run', real))
        self.addCleanup(lambda: setattr(builder_resources.subprocess, 'check_output',
                                        self.real_check_output))

    def test_absent_owned_container_is_terminal_and_attributed(self):
        result = self.observer.cleanup_owned(defer_removal=True)
        self.assertIs(result['retained_terminal'], True,
                      'Harbor teardown must not look like failed retention')
        row = result['containers'][0]
        self.assertIs(row['absent_after_cleanup'], True)
        self.assertIs(row['retained_terminal'], True)
        self.assertIs(row['removed_by_environment_teardown'], True)
        self.assertIs(row['ownership_proven_at_observation'], True)

    def test_declaration_excludes_absent_and_records_it_separately(self):
        (self.run / 'builder_container_cleanup.json').write_text(json.dumps({
            'complete': False, 'removal_deferred': True, 'retained_terminal': True,
            'containers': [{'container_id': OWNED, 'absent_after_cleanup': True,
                            'retained_terminal': True, 'removed_by_environment_teardown': True}],
            'networks': []}))

        class Broker:
            container_id = BROKER

        value = readiness_resources.retained_manifest(self.run, [Broker()])
        self.assertEqual(value['containers'], [BROKER],
                         'a provably absent container must not be handed to the coordinator')
        self.assertEqual([r['container_id'] for r in value['absent_before_declaration']], [OWNED])
        self.assertIn('harbor environment teardown', value['absent_attribution'])
        self.assertIs(value['coordinator_cleanup_required'], True)
        written = json.loads((self.run / 'readiness_retained_resources.json').read_bytes())
        self.assertEqual(written, value)

    def test_unproven_retention_still_fails_closed(self):
        (self.run / 'builder_container_cleanup.json').write_text(json.dumps({
            'complete': False, 'removal_deferred': True, 'retained_terminal': False,
            'containers': [], 'networks': []}))
        with self.assertRaisesRegex(ValueError, 'retention is unproven'):
            readiness_resources.retained_manifest(self.run, [])


if __name__ == '__main__':
    unittest.main()
