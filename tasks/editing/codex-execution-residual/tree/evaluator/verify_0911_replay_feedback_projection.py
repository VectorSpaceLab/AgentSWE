"""Pure projection tests; actual terminal responses are read-only inputs, never replayed."""
from __future__ import annotations
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
import unittest


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ProjectionTests(unittest.TestCase):
    def test_actual_initial_response_preserves_false(self):
        raw = public_record(context, attempt, duplicate=False)
        self.assertIs(raw['replay_allowed'], False)
        self.assertEqual(old.public_payload(raw), actual['item_66']['payload'])
        self.assertEqual(new.public_payload(raw), {**actual['item_66']['payload'], 'replay_allowed': False})

    def test_actual_duplicate_response_preserves_false(self):
        raw = public_record(context, attempt, duplicate=True)
        self.assertEqual(old.public_payload(raw), actual['item_97']['payload'])
        self.assertEqual(new.public_payload(raw), {**actual['item_97']['payload'], 'replay_allowed': False})

    def test_existing_public_record_still_disallows_replay(self):
        for duplicate in (False, True):
            raw = public_record(context, attempt, duplicate=duplicate)
            self.assertEqual(raw['state'], 'infrastructure_error')
            self.assertIs(raw['round_consumed'], False)
            self.assertIs(raw['replay_allowed'], False)
            self.assertEqual(raw['accepted_submissions'], 0)
            self.assertNotIn('score', raw)

    def test_baseline_reproduces_original_projection_defect(self):
        self.assertNotIn('replay_allowed', old.PUBLIC_FIELDS)
        for item in actual.values():
            self.assertEqual(item['status'], 200)
            self.assertNotIn('replay_allowed', item['payload'])

    def test_only_one_field_is_new(self):
        self.assertEqual(new.PUBLIC_FIELDS, old.PUBLIC_FIELDS | {'replay_allowed'})

    def test_false_is_not_dropped_by_truthiness(self):
        self.assertEqual(new.public_payload({'replay_allowed': False}), {'replay_allowed': False})

    def test_true_remains_explicit_boolean(self):
        self.assertEqual(new.public_payload({'replay_allowed': True}), {'replay_allowed': True})

    def test_non_boolean_values_are_not_a_new_data_channel(self):
        for value in (0, 1, None, 'false', 'AUTH_SENTINEL', {'error': 'AUTH_SENTINEL'}, ['RAW_SENTINEL']):
            with self.subTest(value=repr(value)):
                self.assertEqual(new.public_payload({'replay_allowed': value}), {})

    def test_nested_public_infrastructure_response(self):
        value = {'records': [{'state': 'infrastructure_error', 'round_consumed': False,
                             'replay_allowed': False, 'raw_response': 'PRIVATE_SENTINEL'}]}
        self.assertEqual(new.public_payload(value), {'records': [{'state': 'infrastructure_error',
            'round_consumed': False, 'replay_allowed': False}]})

    def test_private_fields_stay_excluded(self):
        private = {key: 'PRIVATE_SENTINEL' for key in ('Authorization', 'auth', 'api_key', 'token',
            'headers', 'request', 'raw', 'raw_body', 'raw_response', 'candidate', 'binary',
            'oracle', 'oracle_summary', 'judge_prompt', 'result_path')}
        self.assertEqual(new.public_payload({**private, 'replay_allowed': False}), {'replay_allowed': False})

    def test_private_paths_still_redacted_and_public_paths_retained(self):
        value = {'error': '@@AGENTSWE_LEGACY_DATA@@/private.json /home/owner/auth.json /tmp/a /root/b src/main.rs /workspace/submission/solution.patch', 'replay_allowed': False}
        projected = new.public_payload(value)
        self.assertEqual(projected['error'], '<evaluator-path> <evaluator-path> <evaluator-path> <evaluator-path> src/main.rs /workspace/submission/solution.patch')

    def test_all_preexisting_fields_have_identical_projection(self):
        for key in old.PUBLIC_FIELDS:
            for value in ('public value /data/private/path', 0, False, None,
                          {'score': 12, 'oracle': 'PRIVATE'}, [{'reason': 'public', 'token': 'PRIVATE'}]):
                with self.subTest(key=key, value=repr(value)):
                    self.assertEqual(new.public_payload({key: value}), old.public_payload({key: value}))

    def test_completed_feedback_projection_regression(self):
        record = {**attempt, 'state': 'completed', 'dev_score': 70, 'dev_scores': {'dev_001': 60, 'dev_002': 80},
                  'dev_passed': True, 'feedback_text': 'Authoritative public feedback', 'feedback_digest': 'f'*64}
        payload = public_record(context, record)
        self.assertEqual(new.public_payload(payload), old.public_payload(payload))
        self.assertNotIn('replay_allowed', new.public_payload(payload))

    def test_semantic_feedback_projection_regression(self):
        contract = {'assessment': 'Observed public task result', 'major_errors': ['Missing transaction receipt'],
            'dimensions': {'task_completion': {'score': 12, 'max': 60, 'rationale': 'incomplete'}},
            'oracle_summary': 'PRIVATE_ORACLE', 'judge_prompt': 'PRIVATE_PROMPT', 'raw_response_path': '/data/private'}
        self.assertEqual(new.semantic_feedback(contract), old.semantic_feedback(contract))
        self.assertNotIn('PRIVATE_', json.dumps(new.semantic_feedback(contract)))

    def test_projection_is_idempotent(self):
        value = new.public_payload(public_record(context, attempt))
        self.assertEqual(new.public_payload(value), value)

    def test_projection_does_not_mutate_actual_record(self):
        before = copy.deepcopy(attempt)
        new.public_payload(public_record(context, attempt))
        self.assertEqual(attempt, before)

    def test_scalar_and_unknown_object_regression(self):
        for value in (None, True, 0, 2.5, 'public', object()):
            self.assertEqual(new.public_payload(value), old.public_payload(value))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--formal-source', type=Path, required=True)
    parser.add_argument('--attempt', type=Path, required=True)
    parser.add_argument('--native-stream', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    staged = Path(__file__).resolve().parents[1] / 'harbor/public_feedback.py'
    old = load_module('replay_projection_baseline', args.baseline)
    new = load_module('replay_projection_stage', staged)
    attempt = json.loads(args.attempt.read_text())
    actual = {}
    for line in args.native_stream.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('id') in ('item_66', 'item_97'):
            actual[item['id']] = json.loads(item['aggregated_output'])
    assert set(actual) == {'item_66', 'item_97'}
    # Execute only the exact production pure record->payload method. Never
    # instantiate the controller, import a launcher, or call submit/evaluate.
    tree = ast.parse(args.formal_source.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DevController')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'public')
    namespace = {'Any': Any}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(args.formal_source), 'exec'), namespace)
    public_record = namespace['public']
    context = SimpleNamespace(records=[], frozen=None, max_dev_rounds=10)
    input_paths = [args.baseline, args.formal_source, args.attempt, args.native_stream, staged, Path(__file__)]
    before = {str(path): sha(path) for path in input_paths}
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProjectionTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unchanged = all(sha(path) == value for path, value in before.items())
    report = {'valid': result.wasSuccessful() and unchanged, 'tests_run': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'external_api_calls': 0,
        'controller_or_evaluator_instantiated': False, 'actual_responses_read_only': ['item_66', 'item_97'],
        'input_source_hashes': before, 'all_inputs_unchanged': unchanged,
        'preexisting_public_fields_checked': len(old.PUBLIC_FIELDS),
        'public_method_source_sha256': hashlib.sha256(ast.get_source_segment(args.formal_source.read_text(), method).encode()).hexdigest(),
        'actual_projected_responses': {key: {**value, 'payload': new.public_payload(public_record(context, attempt, key == 'item_97'))} for key, value in actual.items()},
        'production_guard_modified': False, 'historical_requests_replayed': False}
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    raise SystemExit(0 if report['valid'] else 1)
