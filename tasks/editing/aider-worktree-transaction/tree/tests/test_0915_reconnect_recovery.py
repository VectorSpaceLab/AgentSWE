"""Bounded, recorded native reconnects are admissible; unbounded or unrecovered are not.

Provider-free. Locks the 2026-09-15 correction: requiring zero reconnects made
admission depend on a perfect multi-hour provider session, because every native
turn that has ever completed against this provider needed 12-18 of them.
"""
import importlib.util
import os
import unittest
from pathlib import Path

def _exporter():
    """Default to this task's own exporter so plain test discovery works."""
    override = os.environ.get('AGENTSWE_EXPORTER_UNDER_TEST')
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / 'evaluator' / 'readiness_bundle.py'
        if candidate.is_file():
            return candidate
    raise unittest.SkipTest('task exporter not found for reconnect recovery test')


EXPORTER = _exporter()
spec = importlib.util.spec_from_file_location('exporter_under_test', EXPORTER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def native(count, recovered=True, **extra):
    termination = {'native_retry_announcements': count, 'recovered_in_same_turn': recovered}
    termination.update(extra)
    return {'native_termination': termination}


class ReconnectRecoveryTests(unittest.TestCase):
    def test_zero_reconnects_still_admissible(self):
        self.assertIs(mod._reconnects_recovered(native(0, recovered=False)), True)

    def test_observed_real_world_counts_are_admissible(self):
        # 12, 14, 15, 16 and 18 are the counts of the five native turns that have
        # actually completed against this provider.
        for count in (1, 12, 14, 15, 16, 18, mod.MAX_NATIVE_RECONNECTS):
            self.assertIs(mod._reconnects_recovered(native(count)), True, count)

    def test_unrecovered_reconnects_are_rejected(self):
        self.assertIs(mod._reconnects_recovered(native(3, recovered=False)), False)
        self.assertIs(mod._reconnects_recovered(native(3, recovered=None)), False)

    def test_budget_is_bounded(self):
        self.assertIs(mod._reconnects_recovered(native(mod.MAX_NATIVE_RECONNECTS + 1)), False)

    def test_malformed_counts_fail_closed(self):
        self.assertIs(mod._reconnects_recovered(native(-1)), False)
        self.assertIs(mod._reconnects_recovered(native('3')), False)
        self.assertIs(mod._reconnects_recovered(native(True)), False)
        self.assertIs(mod._reconnects_recovered({}), True)  # absent == zero reconnects
        self.assertIs(mod._reconnects_recovered({'native_termination': None}), True)


if __name__ == '__main__':
    unittest.main()
