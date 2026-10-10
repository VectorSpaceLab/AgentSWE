import json
from pathlib import Path
import tempfile
import unittest

import readiness_binding as binding


class DispatchBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        (self.source / 'meta').mkdir(parents=True)
        self.code = self.source / 'controller.py'
        self.code.write_text('value = 1\n')
        self.contract = self.source / 'meta/0905_case_contract.json'
        self.contract.write_text(json.dumps({'task': 't', 'sibling': str(self.source)}))
        (self.root / 'formal_config.py').write_text('from pathlib import Path\nTASKS={"t":Path(' + repr(str(self.source)) + ')}\n')
        self.registry = self.root / 'configuration_delta_registry.json'
        self.registry.write_text(json.dumps({'schema_version': 'agentswe-edit-configuration-registry/v1',
            'tasks': {'t': {'sibling': str(self.source), 'status': 'REPAIR',
                            'effective_source_files': [{'path': str(self.code), 'sha256': binding.sha(self.code)}]}}}))
        self.expected = {'task': 't', 'source_digest': binding.tree_digest(self.source),
                         'contract_digest': binding.sha(self.contract),
                         'registry_digest': binding.registry_digest(self.registry, 't')}
        (self.root / 'post_repair_tree_snapshot.json').write_text(json.dumps({'tasks': {'t': {'sibling': {'digest': self.expected['source_digest']}}}}))

    def verify(self):
        return binding.verify_binding(self.source, self.expected, control_root=self.root)

    def test_current_repair_binding_allows_readiness_without_granting_ready(self):
        before = self.registry.read_bytes()
        self.assertEqual(self.verify(), self.expected)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.root / 'formal_readiness_gate.json').exists())

    def test_changed_source_rejected(self):
        self.code.write_text('value = 2\n')
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.verify()

    def test_fresh_hash_does_not_hide_stale_effective_ref(self):
        self.code.write_text('value = 2\n')
        self.expected['source_digest'] = binding.tree_digest(self.source)
        with self.assertRaisesRegex(ValueError, 'effective source drift'):
            self.verify()

    def test_registry_replacement_rejected(self):
        self.registry.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'registry schema'):
            self.verify()

    def test_own_row_change_rejected(self):
        value = json.loads(self.registry.read_text())
        value['tasks']['t']['status'] = 'REPAIR-touched'
        self.registry.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.verify()

    def test_another_task_row_does_not_affect_this_one(self):
        # The point of digesting one row rather than the whole file: a rebind
        # performed for another task used to invalidate this task's binding, and
        # -- because admission recomputes the binding from current bytes -- its
        # already finished bundle with it.
        value = json.loads(self.registry.read_text())
        value['tasks']['other'] = {'sibling': '/nowhere', 'status': 'REPAIR',
                                   'effective_source_files': [{'path': '/nowhere/x.py', 'sha256': '0' * 64}]}
        value['scope'] = 'rewritten by a later window'
        self.registry.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.verify()   # ten-task inventory is still enforced
        value['tasks'].pop('other')
        self.registry.write_text(json.dumps(value))
        self.assertEqual(self.verify(), self.expected)

    def test_snapshot_must_be_refreshed(self):
        (self.root / 'post_repair_tree_snapshot.json').write_text('{"tasks":{"t":{"sibling":{"digest":"old"}}}}')
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            self.verify()

    def test_wrong_task_and_extra_fields_rejected(self):
        self.expected['task'] = 'foreign'
        with self.assertRaises(ValueError):
            self.verify()
        self.expected['task'] = 't'
        self.expected['source_unchanged'] = True
        with self.assertRaises(ValueError):
            self.verify()

    def test_native_retry_policy_only_changes_known_provider_section(self):
        path = self.root / 'provider.toml'
        text = ('model_provider="gateway_direct"\nmodel="deepseek-flash"\nmodel_reasoning_effort="max"\n'
                '[model_providers.gateway_direct]\nname="GATEWAY"\nwire_api="responses"\n')
        path.write_text(text)
        value = binding.configure_native_transport(path)
        self.assertEqual(path.read_text(), text + 'request_max_retries=10\n'
                         'stream_max_retries=10\nstream_idle_timeout_ms=300000\n')
        self.assertEqual(value['provider_config_sha256'], binding.sha(path))
        self.assertEqual((value['request_max_retries'], value['stream_max_retries'],
                          value['stream_idle_timeout_ms']), (10, 10, 300000))
        self.assertIs(value['already_configured'], False)
        path.write_text(text + '[features]\napps=true\n')
        with self.assertRaises(ValueError):
            binding.configure_native_transport(path)

    def test_repeat_configuration_is_idempotent_but_divergence_fails_closed(self):
        path = self.root / 'provider.toml'
        text = ('model_provider="gateway_direct"\nmodel="deepseek-flash"\nmodel_reasoning_effort="max"\n'
                '[model_providers.gateway_direct]\nname="GATEWAY"\nwire_api="responses"\n')
        path.write_text(text)
        first = binding.configure_native_transport(path)
        digest = binding.sha(path)
        second = binding.configure_native_transport(path)
        self.assertIs(second['already_configured'], True)
        self.assertEqual(binding.sha(path), digest)
        self.assertEqual(second['request_max_retries'], first['request_max_retries'])
        path.write_text(text + 'request_max_retries=0\nstream_max_retries=0\n'
                        'stream_idle_timeout_ms=0\n')
        with self.assertRaisesRegex(ValueError, 'retry configuration differs'):
            binding.configure_native_transport(path)

    def test_legacy_no_replay_name_still_resolves(self):
        self.assertIs(binding.configure_native_no_replay, binding.configure_native_transport)


if __name__ == '__main__':
    unittest.main()
