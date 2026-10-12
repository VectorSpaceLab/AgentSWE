from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import cleanup_compose_residual as cleanup


def result(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


class CleanupEvidenceTests(unittest.TestCase):
    def test_explicit_container_absence(self):
        with patch.object(cleanup, 'command', return_value=result(1, err='Error: No such container: c')):
            self.assertIsNone(cleanup.inspect_json('container', 'c'))

    def test_daemon_failure_not_absence(self):
        with patch.object(cleanup, 'command', return_value=result(1, err='Cannot connect to Docker daemon')):
            with self.assertRaises(RuntimeError):
                cleanup.inspect_json('container', 'c')

    def test_bad_json_not_absence(self):
        for text in ('broken', '[]', '{}'):
            with self.subTest(text=text), patch.object(cleanup, 'command', return_value=result(out=text)):
                with self.assertRaises(RuntimeError):
                    cleanup.inspect_json('network', 'n')

    def test_network_uses_network_inspector(self):
        with patch.object(cleanup, 'command', return_value=result(out='[{"Id":"n"}]')) as cmd:
            self.assertEqual(cleanup.inspect_json('network', 'n')['Id'], 'n')
            cmd.assert_called_once_with(['docker', 'network', 'inspect', 'n'])

    def unit(self, **changes):
        value = dict(LoadState='loaded', ActiveState='inactive', SubState='dead', MainPID='0',
                     ExecMainPID='123', ExecMainExitTimestamp='Mon 2026-09-14 16:00:00 CST',
                     ExecStart='{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 runner.py --run-dir /data/task/run ; }')
        value.update(changes)
        return result(out='\n'.join(k + '=' + v for k, v in value.items()))

    def test_only_same_terminal_unit_passes(self):
        with patch.object(cleanup, 'command', return_value=self.unit()):
            self.assertEqual(cleanup.require_terminal_unit('job.service', Path('/data/task/run'))['MainPID'], '0')

    def test_live_missing_or_foreign_unit_refuses(self):
        for value in (self.unit(ActiveState='active', MainPID='123'), self.unit(LoadState='not-found'),
                      self.unit(ExecMainExitTimestamp='n/a'), self.unit(ExecMainPID='0'),
                      self.unit(ExecStart='runner.py --run-dir /data/task/run-foreign'),
                      result(1, err='Timed out')):
            with self.subTest(value=value), patch.object(cleanup, 'command', return_value=value):
                with self.assertRaises(RuntimeError):
                    cleanup.require_terminal_unit('job.service', Path('/data/task/run'))


if __name__ == '__main__':
    unittest.main()
