"""Actual Git/small Rust build and durable controller controls; no model requests."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'harbor'))
from harbor import formal_one_stop as f
from product_attempts import ProductAttempt, ProductReplayError, product_source_digest

RUNTIME = Path('@@AGENTSWE_ENVS@@/codex-project-memory-edit-v1')
BASE = 'fn main() { println!("baseline"); }\n'


class ProductControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.benchmark = self.root / 'benchmark'
        repo = self.benchmark / 'input/repository'
        (repo / 'codex-rs').mkdir(parents=True)
        (repo / 'codex-rs/main.rs').write_text(BASE)
        (repo / 'codex-rs/Cargo.lock').write_text('# synthetic healthy compiler control\n')
        self.workspace = self.root / 'submission'; self.workspace.mkdir()
        self.delivery('first')
        self.run = self.root / 'run'; self.run.mkdir()
        self.stack = []
        self.add_patch(patch.object(f, 'check_build_environment', return_value={'valid': True}))
        self.add_patch(patch.object(f, 'toolchain_identity', return_value={'sha256': 'synthetic-fixed-toolchain'}))
        self.add_patch(patch.object(f, 'private_candidate_target', side_effect=self.private_target))
        self.add_patch(patch.object(f, 'isolated_build_command', side_effect=self.small_rust_command))
        self.add_patch(patch.object(f.DevController, 'bind_native_thread', lambda self: None))

    def add_patch(self, context):
        self.stack.append(context); return context.start()

    def tearDown(self):
        for context in reversed(self.stack): context.stop()
        self.temp.cleanup()

    def delivery(self, value):
        (self.workspace / 'solution.patch').write_text(
            'diff --git a/codex-rs/main.rs b/codex-rs/main.rs\n'
            '--- a/codex-rs/main.rs\n+++ b/codex-rs/main.rs\n@@ -1 +1 @@\n'
            '-' + BASE + '+fn main() { println!("' + value + '"); }\n')
        for name in ['edit_report.json', 'run_report.json']:
            (self.workspace / name).write_text('{}')

    def private_target(self, baseline, output):
        target = output / 'synthetic-target'; (target / 'debug').mkdir(parents=True)
        return target

    def small_rust_command(self, original, *, runtime, target, worktree):
        # This control verifies actual apply/build identity, not full Cargo
        # dependencies or the production bwrap contract (covered separately).
        return [str(RUNTIME / 'bin/rustc'), '--edition=2021',
                str(worktree / 'codex-rs/main.rs'), '-o', str(target / 'debug/codex')]

    def build(self, output):
        return f.build_candidate(benchmark=self.benchmark, snapshot=self.workspace,
            output=output, runtime=RUNTIME, shared_target=self.root / 'baseline-target')

    def evaluate(self, number, snapshot):
        return f.evaluate_public_candidate(benchmark=self.benchmark, run_dir=self.run,
            number=number, snapshot=snapshot, runtime=RUNTIME,
            shared_target=self.root / 'baseline-target', cases=('dev_001', 'dev_002'),
            lower_endpoint='http://unused.invalid', judge_endpoint='http://unused.invalid')

    def commit_baseline(self, date):
        repo = self.benchmark / 'input/repository'
        env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        for args in [['git', 'init', '-q'], ['git', 'add', '-A'],
                     ['git', '-c', 'user.name=Control', '-c', 'user.email=control@example.invalid',
                      'commit', '--allow-empty', '-q', '-m', 'synthetic fixture']]:
            subprocess.run(args, cwd=repo, env=env, check=True, capture_output=True)

    def test_actual_materialization_and_rust_build_ignore_only_git_dates_and_reports(self):
        self.commit_baseline('2001-01-01T00:00:00Z')
        binary, a = self.build(self.root / 'build-one')
        self.assertEqual(subprocess.check_output([str(binary)], text=True).strip(), 'first')
        self.commit_baseline('2002-01-01T00:00:00Z')
        (self.workspace / 'run_report.json').write_text('{"changed":"report only"}')
        _, b = self.build(self.root / 'build-two')
        self.assertNotEqual(a['candidate_digest'], b['candidate_digest'])
        self.assertNotEqual(a['candidate_repo_digest'], b['candidate_repo_digest'])
        self.assertEqual(a['product_source_digest'], b['product_source_digest'])
        self.assertEqual(a['product_source_digest_stage'], 'after_patch_apply_before_compilation')
        self.delivery('real revision')
        _, c = self.build(self.root / 'build-three')
        self.assertNotEqual(a['product_source_digest'], c['product_source_digest'])

    def test_completed_and_unknown_results_preserved_across_all_repeat_routes(self):
        calls = []
        def lower(**kwargs):
            case = kwargs['case'].name; calls.append(case)
            owners = list((self.run / 'product_attempts').glob('*/owner.json'))
            self.assertEqual(len(owners), 1)
            self.assertTrue((owners[0].parent / case / 'intent.json').is_file())
            return {'case_id': case, 'score': 99 if case == 'dev_001' else None,
                'classification': 'completed' if case == 'dev_001' else 'provider_infrastructure_failure',
                'broker': {'calls_delta': 1, 'tokens_delta': 7 if case == 'dev_001' else None}}
        def score(**kwargs):
            if kwargs['case_id'] == 'dev_002':
                raise f.BuildInfrastructureError('synthetic delivery unknown')
            return kwargs['result']
        with patch.object(f, 'run_agent_case', side_effect=lower), patch.object(f, 'score_public_execution', side_effect=score):
            controller = f.DevController(run_dir=self.run, workspace=self.workspace, evaluate=self.evaluate)
            controller.submit(); controller.wait_idle()
            ledger = next((self.run / 'product_attempts').iterdir())
            known = ledger / 'dev_001/completed_result.json'
            unknown = ledger / 'dev_002/lower_result.json'
            preserved = (known.read_bytes(), unknown.read_bytes())
            self.assertEqual(json.loads(preserved[0])['score'], 99)
            self.assertIsNone(json.loads(preserved[1])['broker']['tokens_delta'])
            code, _ = controller.submit(); controller.wait_idle()
            self.assertEqual(code, 200)
            (self.workspace / 'run_report.json').write_text('{"report":"only"}')
            controller.submit(); controller.wait_idle()
            with self.assertRaises(f.BuildInfrastructureError):
                f.DevController(run_dir=self.run, workspace=self.workspace, evaluate=self.evaluate)
            with self.assertRaises(ProductReplayError):
                ProductAttempt(self.run / 'product_attempts', ledger.name, {'restart': True})
            self.assertEqual(calls, ['dev_001', 'dev_002'])
            self.assertEqual((known.read_bytes(), unknown.read_bytes()), preserved)
            self.assertEqual(len(controller.records), 0)

    def test_completed_report_change_blocked_and_real_product_revision_allowed(self):
        calls = []
        def lower(**kwargs):
            calls.append(kwargs['case'].name)
            return {'case_id': kwargs['case'].name, 'score': 88, 'classification': 'completed'}
        with patch.object(f, 'run_agent_case', side_effect=lower), patch.object(f, 'score_public_execution', side_effect=lambda **k: k['result']):
            c = f.DevController(run_dir=self.run, workspace=self.workspace, evaluate=self.evaluate)
            c.submit(); c.wait_idle(); feedback = c.records[-1]['feedback_digest']
            (self.workspace / 'run_report.json').write_text('{"report":"only"}')
            c.submit(feedback); c.wait_idle()
            self.assertEqual(len(c.records), 1); self.assertEqual(len(calls), 2)
            self.delivery('second substantive implementation')
            c.submit(feedback); c.wait_idle()
            self.assertEqual(len(c.records), 2); self.assertEqual(len(calls), 4)
            self.assertEqual(len(list((self.run / 'product_attempts').iterdir())), 2)

    def test_known_compile_error_zero_still_completes_without_lower(self):
        (self.workspace / 'solution.patch').write_text(
            'diff --git a/codex-rs/main.rs b/codex-rs/main.rs\n'
            '--- a/codex-rs/main.rs\n+++ b/codex-rs/main.rs\n@@ -1 +1 @@\n'
            '-' + BASE + '+fn main() { let _: () = 123; }\n')
        with patch.object(f, 'run_agent_case', side_effect=AssertionError('compile zero must not call lower')):
            c = f.DevController(run_dir=self.run, workspace=self.workspace, evaluate=self.evaluate)
            c.submit(); c.wait_idle()
            self.assertEqual(c.records[0]['dev_scores'], {'dev_001': 0, 'dev_002': 0})
            self.assertEqual(len(list((self.run / 'product_attempts').iterdir())), 1)
            frozen = c.freeze_latest('builder_exit')
            self.assertEqual(frozen['accepted_rounds'], 1)

    def test_only_root_git_is_excluded_and_link_target_not_followed(self):
        root = self.root / 'source'; root.mkdir(); (root / 'code').write_text('one')
        (root / '.git').mkdir(); (root / '.git/HEAD').write_text('old')
        before = product_source_digest(root); (root / '.git/HEAD').write_text('new')
        self.assertEqual(before, product_source_digest(root))
        (root / 'nested/.git').mkdir(parents=True); (root / 'nested/.git/data').write_text('included')
        self.assertNotEqual(before, product_source_digest(root))
        secret = self.root / 'outside'; secret.write_text('do not follow')
        (root / 'link').symlink_to(secret); before = product_source_digest(root)
        secret.write_text('changed outside source'); self.assertEqual(before, product_source_digest(root))

    def test_partial_or_corrupt_reservation_never_dispatches_again(self):
        digest = 'a' * 64; root = self.root / 'attempts'; (root / digest).mkdir(parents=True)
        for value in [None, b'', b'{broken']:
            if value is not None: (root / digest / 'owner.json').write_bytes(value)
            with self.assertRaises(ProductReplayError): ProductAttempt(root, digest, {})

    def test_concurrent_claim_has_exactly_one_owner(self):
        root = self.root / 'attempts'
        def claim(_):
            try: ProductAttempt(root, 'b' * 64, {}); return True
            except ProductReplayError: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(claim, range(4))), 1)


if __name__ == '__main__': unittest.main()
