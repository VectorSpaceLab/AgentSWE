"""Stage-scoped recovery and no-resampling regressions; all work synthetic."""
import copy
import errno
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_v32_dev_execution as fixtures
from agentloop.stage_recovery import CaseStages, immutable_json
from agentloop.protocol import write_json


class StageRecoveryTests(unittest.TestCase):
    setUp = fixtures.DevExecutionTests.setUp
    patch = fixtures.DevExecutionTests.patch
    reload = fixtures.DevExecutionTests.reload
    write_delivery = fixtures.DevExecutionTests.write_delivery
    fake_build = staticmethod(fixtures.DevExecutionTests.fake_build)
    fake_lower = fixtures.DevExecutionTests.fake_lower
    fake_score = staticmethod(fixtures.DevExecutionTests.fake_score)
    submit = fixtures.DevExecutionTests.submit
    fail_scoring = fixtures.DevExecutionTests.fail_scoring

    def fail_second_scorer_once(self):
        counts = {}
        def score(**kwargs):
            case = kwargs['case_id']
            counts[case] = counts.get(case, 0) + 1
            if case == 'dev_002' and counts[case] == 1:
                raise RuntimeError('synthetic pre-request scoring preparation error')
            return self.fake_score(**kwargs)
        self.scorer.side_effect = score
        return counts

    def test_recovery_reuses_completed_lower_and_other_case_score(self):
        counts = self.fail_second_scorer_once()
        first = self.submit()
        self.assertFalse(first['accepted'])
        first_record = Path(first['attempt_paths']['record']).read_bytes()
        second = self.submit()
        self.assertTrue(second['accepted'], second)
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.lower.call_count, 2)
        self.assertEqual(counts, {'dev_001': 1, 'dev_002': 2})
        self.assertEqual(first['attempt_paths']['root'], second['attempt_paths']['root'])
        self.assertEqual(Path(first['attempt_paths']['record']).read_bytes(), first_record)
        self.assertEqual(second['previous_record']['path'], first['attempt_paths']['record'])
        self.assertEqual(len(self.controller.records), 1)
        self.assertEqual(len(self.controller.infrastructure_attempts), 1)

    def test_reloaded_controller_recovers_same_stages(self):
        counts = self.fail_second_scorer_once()
        self.submit()
        self.controller = self.reload()
        second = self.submit()
        self.assertTrue(second['accepted'], second)
        self.assertEqual((self.build.call_count, self.lower.call_count), (1, 2))
        self.assertEqual(counts, {'dev_001': 1, 'dev_002': 2})

    def test_retry_cannot_change_builder_session(self):
        self.fail_second_scorer_once()
        self.submit()
        value = self.controller.submit(self.workspace, builder_session_id='different-session')
        self.assertFalse(value['accepted'])
        self.assertIn('session changed', value['details'])
        self.assertEqual(self.lower.call_count, 2)

    def test_retry_cannot_change_execution_identity(self):
        self.fail_second_scorer_once()
        self.submit()
        self.controller.image = 'changed-runtime-image'
        value = self.submit()
        self.assertFalse(value['accepted'])
        self.assertIn('identity changed', value['details'])
        self.assertEqual(self.lower.call_count, 2)

    def test_prior_attempt_record_tamper_is_not_adopted(self):
        self.fail_second_scorer_once()
        first = self.submit()
        write_json(Path(first['attempt_paths']['record']), {'tampered': True})
        value = self.submit()
        self.assertFalse(value['accepted'])
        self.assertIn('record changed', value['details'])
        self.assertEqual(self.lower.call_count, 2)

    def test_prior_lower_evidence_tamper_does_not_trigger_new_rollout(self):
        self.fail_second_scorer_once()
        first = self.submit()
        (Path(first['dev']['dev_001']['output_path']) / 'product.txt').write_text('changed')
        value = self.submit()
        self.assertFalse(value['accepted'])
        self.assertIn('lower evidence changed', value['dev']['dev_001']['error'])
        self.assertEqual(self.lower.call_count, 2)

    def test_prior_completed_score_evidence_tamper_is_not_rejudged(self):
        counts = self.fail_second_scorer_once()
        first = self.submit()
        output = Path(first['dev']['dev_001']['output_path']) / 'semantic_result'
        write_json(output / 'unexpected.json', {'changed': True})
        value = self.submit()
        self.assertFalse(value['accepted'])
        self.assertIn('completed scoring evidence changed', value['dev']['dev_001']['error'])
        self.assertEqual(counts['dev_001'], 1)
        self.assertEqual(self.lower.call_count, 2)

    def test_unknown_interrupted_lower_blocks_an_untracked_second_attempt(self):
        def interrupted(command, **kwargs):
            self.fake_lower(command, **kwargs)
            raise KeyboardInterrupt('synthetic loss of controller while lower outcome is unknown')
        self.lower.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.submit()
        self.controller = self.reload()
        value = self.submit()
        self.assertFalse(value['accepted'])
        self.assertEqual(value['error'], 'uncommitted_attempt_requires_recovery')
        self.assertEqual(self.lower.call_count, 1)

    def test_case_stage_unknown_intent_never_invokes_lower(self):
        stages = CaseStages(self.root / 'unknown-case', {'synthetic': True})
        immutable_json(stages.root / 'lower_001' / 'intent.json', {'invocation': 1})
        with self.assertRaisesRegex(RuntimeError, 'in-flight or unknown'):
            stages.lower(lambda: self.fail('must not restart an unknown invocation'))

    def test_verified_prelaunch_error_retries_only_that_lower_stage(self):
        def lower(command, **kwargs):
            if self.lower.call_count == 1:
                raise FileNotFoundError(errno.ENOENT, 'synthetic exec failure', command[0])
            return self.fake_lower(command, **kwargs)
        self.lower.side_effect = lower
        first = self.submit()
        self.assertFalse(first['accepted'])
        second = self.submit()
        self.assertTrue(second['accepted'], second)
        self.assertEqual(self.lower.call_count, 3)
        self.assertEqual(self.scorer.call_count, 2)
        self.assertEqual(self.build.call_count, 1)

    def test_verified_prelaunch_retry_bound_cannot_loop_forever(self):
        def lower(command, **kwargs):
            if Path(command[command.index('--case-file') + 1]).parent.name == 'dev_001':
                raise FileNotFoundError(errno.ENOENT, 'synthetic missing executable', command[0])
            return self.fake_lower(command, **kwargs)
        self.lower.side_effect = lower
        for _ in range(5):
            self.assertFalse(self.submit()['accepted'])
        self.assertEqual(self.lower.call_count, 4)  # three dev_001 starts, one dev_002
        self.assertEqual(self.scorer.call_count, 1)
        self.assertEqual(self.controller.records, [])

    def test_unclassified_oserror_after_possible_start_is_never_replayed(self):
        def lower(command, **kwargs):
            if self.lower.call_count == 1:
                raise OSError(errno.EIO, 'synthetic pipe failure after possible child start')
            return self.fake_lower(command, **kwargs)
        self.lower.side_effect = lower
        first = self.submit()
        self.assertFalse(first['accepted'])
        self.assertFalse(first['dev']['dev_001']['dispatch_not_started'])
        second = self.submit()
        self.assertFalse(second['accepted'])
        self.assertEqual(self.lower.call_count, 2)
        self.assertEqual(self.scorer.call_count, 1)

    def test_private_execution_identity_is_not_returned_to_builder(self):
        value = self.submit()
        private_identity = json.loads((Path(value['attempt_paths']['root']) / 'execution_identity.json').read_text())
        self.assertIn('runtime_sources', private_identity)
        encoded = json.dumps(value)
        self.assertNotIn('runtime_sources', encoded)
        self.assertNotIn('test_006', encoded)

    def test_changed_candidate_still_receives_its_own_evaluation(self):
        self.fail_second_scorer_once()
        first = self.submit()
        self.write_delivery(2)
        self.scorer.side_effect = self.fake_score
        second = self.submit()
        self.assertTrue(second['accepted'], second)
        self.assertNotEqual(first['candidate_digest'], second['candidate_digest'])
        self.assertNotEqual(first['attempt_paths']['root'], second['attempt_paths']['root'])
        self.assertEqual(self.lower.call_count, 4)

    def test_second_controller_cannot_evaluate_same_run_concurrently(self):
        other = self.reload()
        entered, release = threading.Event(), threading.Event()
        def lower(command, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test synchronization timeout')
            return self.fake_lower(command, **kwargs)
        self.lower.side_effect = lower
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.submit()))
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            value = other.submit(self.workspace, builder_session_id='synthetic-v32-session')
            self.assertFalse(value['accepted'])
            self.assertEqual(value['error'], 'submission_already_active')
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(outcomes[0]['accepted'])
        self.assertEqual(self.lower.call_count, 2)

    def test_real_shared_scoring_intent_does_not_repeat_unknown_request(self):
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        import execution_scoring
        out = self.root / 'shared-case'
        stages = CaseStages(out, {'synthetic_unit_fixture': True})
        source = self.root / 'scoring-inputs'
        source.mkdir()
        inputs = {}
        for name in ('case_input', 'rubric', 'artifact', 'raw_trajectory', 'native_evidence', 'private_oracle'):
            inputs[name] = source / (name + '.txt')
            inputs[name].write_text('explicitly synthetic scoring input\n')
        digest = 'a' * 64
        record = {'case_id': 'dev_001', 'candidate_digest': digest, 'real_execution': True,
            'broker': {'calls_delta': 1, 'successful_calls': 1},
            'artifact_validation': {'validated_by': 'evaluator', 'valid': True,
                                    'sha256': hashlib.sha256(inputs['artifact'].read_bytes()).hexdigest()}}
        def evaluate():
            return execution_scoring.judge_execution_case(**inputs, execution_record=record,
                candidate_digest=digest, case_id='dev_001', output=out / 'semantic_result',
                broker_endpoint='http://unused.invalid')
        def uncertain(command, **_kwargs):
            destination = Path(command[command.index('--output-dir') + 1])
            write_json(destination / 'result_score_contract.json', {'evaluation_state': 'infrastructure_error',
                'synthetic_unit_fixture': True, 'provider_usage': {'logical_requests': 1, 'completed_responses': 0}})
            return subprocess.CompletedProcess(command, 1, '', 'synthetic unknown response outcome')
        with patch.object(execution_scoring.subprocess, 'run', side_effect=uncertain) as runner:
            first = stages.score(evaluate)
            intent = (out / 'semantic_result' / 'scoring_intent.json').read_bytes()
            second = stages.score(evaluate)
            self.assertFalse(first['contract_valid'])
            self.assertFalse(second['contract_valid'])
            self.assertEqual(runner.call_count, 1)
            self.assertIn('requires recovery', second['reason'])
            self.assertEqual((out / 'semantic_result' / 'scoring_intent.json').read_bytes(), intent)


if __name__ == '__main__':
    unittest.main()
