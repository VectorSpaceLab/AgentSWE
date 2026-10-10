"""Provider-free build attribution controls; integration is a separate verifier."""
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harbor.build_preflight import classify, diagnostics, verify_container, IMAGE, LABEL, MEMORY_BYTES


def observed(text='', exit_code=0):
    return {'exit_code': exit_code, 'diagnostics': diagnostics(text),
            'resource_attestation': {'valid': True, 'cleanup': {'complete': True}, 'oom_killed': False}}

ERROR = "src/probe.ts(1,1): error TS2322: Type 'string' is not assignable to type 'number'."


class BuildTests(unittest.TestCase):
    def test_repeated_leaf_output_deduplicates_only_exact_locations(self):
        self.assertEqual(len(diagnostics(ERROR + '\n' + ERROR)), 1)
        self.assertEqual(len(diagnostics(ERROR + '\n' + ERROR.replace('(1,1)', '(2,1)'))), 2)

    def test_clean_baseline_new_fatal_is_candidate(self):
        baseline = classify(observed())
        value = classify(observed(ERROR, 2), baseline)
        self.assertEqual(value['classification'], 'candidate_build_failure')
        self.assertFalse(value['ready_for_submission'])

    def test_existing_errors_and_line_shifts_are_compatible(self):
        baseline = classify(observed(ERROR, 2))
        self.assertFalse(baseline['required_pass'])
        value = classify(observed(ERROR.replace('(1,1)', '(19,1)'), 2), baseline)
        self.assertEqual(value['classification'], 'ready_for_lower')

    def test_existing_error_does_not_hide_new_duplicate(self):
        baseline = classify(observed(ERROR, 2))
        value = classify(observed(ERROR + '\n' + ERROR.replace('(1,1)', '(2,1)'), 2), baseline)
        self.assertEqual(value['classification'], 'candidate_build_failure')
        self.assertEqual(len(value['new_diagnostics']), 1)

    def test_required_pass_false_never_opens_blanket_bypass(self):
        baseline = classify(observed(ERROR, 2))
        extra = ERROR.replace('src/probe.ts', 'src/other.ts')
        self.assertEqual(classify(observed(ERROR + '\n' + extra, 2), baseline)['classification'], 'candidate_build_failure')

    def test_missing_dependency_is_infrastructure(self):
        row = "src/probe.ts(1,1): error TS2307: Cannot find module 'missing'."
        self.assertEqual(classify(observed(row, 2), classify(observed()))['classification'], 'infrastructure_invalid')

    def test_timeout_or_oom_with_diagnostics_is_infrastructure(self):
        for reason in ('timeout', 'oom', 'cleanup'):
            value = observed(ERROR, 2)
            if reason == 'timeout': value['timed_out'] = True
            elif reason == 'oom': value['resource_attestation']['oom_killed'] = True
            else: value['resource_attestation']['cleanup']['complete'] = False
            self.assertEqual(classify(value, classify(observed()))['classification'], 'infrastructure_invalid')

    def test_unhealthy_baseline_never_proves_candidate_fault(self):
        self.assertEqual(classify(observed(ERROR, 2), {'required_pass': False})['classification'], 'infrastructure_invalid')

    def test_unrecognized_failure_never_proves_candidate_fault(self):
        for value in [observed('', 2), observed(ERROR, 137), observed(ERROR, 1)]:
            self.assertEqual(classify(value, classify(observed()))['classification'], 'infrastructure_invalid')

    def test_wrong_container_identity_or_resources_refuses_control(self):
        base = {'Id': 'cid', 'Image': IMAGE, 'Config': {'Labels': {LABEL: 'token'}},
                'HostConfig': {'Memory': MEMORY_BYTES, 'MemorySwap': MEMORY_BYTES,
                               'NetworkMode': 'none', 'PidsLimit': 512, 'ReadonlyRootfs': True,
                               'NanoCpus': 8_000_000_000}}
        verify_container(base, 'cid', 'token')
        for field in ['Id', 'Image']:
            value = copy.deepcopy(base); value[field] = 'foreign'
            with self.assertRaises(RuntimeError): verify_container(value, 'cid', 'token')
        value = copy.deepcopy(base); value['HostConfig']['NetworkMode'] = 'host'
        with self.assertRaises(RuntimeError): verify_container(value, 'cid', 'token')


if __name__ == '__main__': unittest.main()
