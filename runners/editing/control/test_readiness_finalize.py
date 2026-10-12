"""Recovery integration: packaging failure must not dispatch cleanup twice."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import readiness_finalize as finalizer


class FinalizeTests(unittest.TestCase):
    def test_export_failure_can_resume_after_verified_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root/'source'
            (source/'evaluator').mkdir(parents=True)
            (source/'evaluator/readiness_bundle.py').touch()
            run = root/'smoke/openwiki/fresh'
            run.mkdir(parents=True)
            binding = root/'binding.json'
            binding.write_text(json.dumps({'task': 'openwiki'}))
            binding_sha = hashlib.sha256(binding.read_bytes()).hexdigest()
            cleanup_path = run/'coordinator_cleanup/cleanup.json'
            cleanup_ref = {'path': str(cleanup_path), 'sha256': 'checked-by-cleanup-test'}
            def clean(*args):
                cleanup_path.parent.mkdir()
                cleanup_path.write_text('{}')
                return cleanup_ref
            outputs = []
            def export(run_dir, destination, **kwargs):
                destination.mkdir()
                outputs.append(destination)
                if len(outputs) == 1:
                    raise ValueError('temporary packaging failure')
                (destination/'manifest.json').write_text('{}')
                return {'bundle_root': str(destination), 'manifest_sha256': hashlib.sha256(b'{}').hexdigest()}
            module = SimpleNamespace(export=export)
            spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda value: None))
            with patch.object(finalizer.cfg, 'TASKS', {'openwiki': source}), \
                    patch.object(finalizer.cfg, 'SMOKE_ROOT', root/'smoke'), \
                    patch.object(finalizer, 'cleanup', side_effect=clean) as cleanup, \
                    patch.object(finalizer, 'verify_completed_cleanup', return_value=cleanup_ref) as verify_cleanup, \
                    patch.object(finalizer, 'verify_binding'), \
                    patch.object(finalizer.importlib.util, 'spec_from_file_location', return_value=spec), \
                    patch.object(finalizer.importlib.util, 'module_from_spec', return_value=module), \
                    patch.object(finalizer, 'make_broker_record_normalizers', return_value={}), \
                    patch.object(finalizer, 'make_judge_output_validators', return_value={}), \
                    patch.object(finalizer, 'load_and_validate', return_value=(True, [])):
                with self.assertRaisesRegex(ValueError, 'packaging failure'):
                    finalizer.finalize('openwiki', run, 'test.service', binding, binding_sha)
                receipt = finalizer.finalize('openwiki', run, 'test.service', binding, binding_sha)
            self.assertEqual(cleanup.call_count, 1)
            self.assertEqual(verify_cleanup.call_count, 1)
            self.assertEqual(len(set(outputs)), 2)
            self.assertTrue(all(p.is_dir() for p in outputs))
            self.assertTrue(receipt['validation_passed'])
            self.assertFalse(receipt['pipeline_ready'])


if __name__ == '__main__':
    unittest.main()
