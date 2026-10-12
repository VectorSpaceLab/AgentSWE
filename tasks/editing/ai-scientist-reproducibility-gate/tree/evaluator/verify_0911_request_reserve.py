"""Explicit zero-provider tests of the trusted launcher and real modern lower HTTP gate."""
import argparse
import ast
import concurrent.futures
import copy
import contextvars
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'agentloop'), str(ROOT/'evaluator')]
from agentloop import lower_request_identity as identity
sys.modules['lower_request_identity'] = identity
from lower_request_reserve import RequestReserve, ReplayForbidden, load_history
import lower_responses_broker as broker
import lower_agent_launcher as launcher
from agentloop.two_round_controller import Controller
from harbor import formal_one_stop as formal

KEY = hashlib.sha256(b'zero-provider-fixture').digest()
assert len(KEY) == 32
PAYLOAD = {'model': 'candidate-overridden', 'reasoning': {'effort': 'candidate-overridden'}, 'input': 'fixture', 'stream': False}
PRODUCT = 'e'*64
CASE = 'c'*64


def signed(slot='action:1', *, product=PRODUCT, payload=None, request_id=None, key=KEY):
    return identity.sign_request(key, product, 'dev_001', CASE, slot, payload or PAYLOAD, request_id=request_id)


def process_reserve(values):
    root, header = values
    registry = RequestReserve(root, KEY)
    try:
        registry.reserve(header, identity.normalized_payload(PAYLOAD))
        return 'admitted'
    except ReplayForbidden:
        return 'blocked'


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, history=None):
        return RequestReserve(self.root/'reserve', KEY, history)

    def reserve(self, registry, header, payload=None):
        return registry.reserve(header, identity.normalized_payload(payload or PAYLOAD),
            legacy_bytes=identity.normalized_payload(payload or PAYLOAD, stream=False))

    def test_valid_context_new_source_is_admitted(self):
        self.assertTrue(self.reserve(self.registry(HISTORY), signed())['signature_verified'])

    def test_all_legal_action_and_authoring_slots_are_distinct(self):
        self.assertEqual(launcher.MAX_ACTION_STEPS, 10)
        self.assertIn('for sequence in range(1, MAX_ACTION_STEPS + 1):', (ROOT/'agentloop/lower_agent_launcher.py').read_text())
        registry = self.registry()
        rows = [self.reserve(registry, signed(slot)) for slot in [*(f'action:{i}' for i in range(1, launcher.MAX_ACTION_STEPS + 1)), 'authoring:1']]
        self.assertEqual(len({r['logical_context_digest'] for r in rows}), launcher.MAX_ACTION_STEPS + 1)

    def test_the_slot_grammar_names_every_slot_the_loop_can_emit(self):
        # D104.  The grammar is the launcher's own step budget plus the one
        # finish-accountability re-ask.  A slot the loop emits but the identity
        # refuses raises ValueError outside the action loop's except tuples
        # (lower_agent_launcher.py:1613,1617) and kills the case.
        self.assertEqual(identity.ACTION_SLOT_STEPS, launcher.MAX_ACTION_STEPS)
        source = (ROOT/'agentloop/lower_agent_launcher.py').read_text()
        self.assertIn('request_slot=f"action:{step}"', source)
        self.assertIn('request_slot=f"accountability:{step}"', source)
        registry = self.registry()
        slots = [*(f'action:{i}' for i in range(1, launcher.MAX_ACTION_STEPS + 1)),
                 *(f'accountability:{i}' for i in range(1, launcher.MAX_ACTION_STEPS + 1)),
                 'authoring:1']
        rows = [self.reserve(registry, signed(slot)) for slot in slots]
        self.assertEqual(len({r['logical_context_digest'] for r in rows}), 2 * launcher.MAX_ACTION_STEPS + 1)
        self.assertEqual([r['request_slot'] for r in rows], slots)

    def test_same_context_new_request_id_is_blocked(self):
        registry = self.registry()
        self.reserve(registry, signed(request_id='1'*32))
        with self.assertRaises(ReplayForbidden):
            self.reserve(registry, signed(request_id='2'*32))

    def test_nonce_paths_and_changed_payload_cannot_release_slot(self):
        registry = self.registry()
        self.reserve(registry, signed())
        value = {**PAYLOAD, 'input': 'new nonce 456 /other/run/output'}
        with self.assertRaises(ReplayForbidden):
            self.reserve(registry, signed(payload=value), value)

    def test_new_product_gets_its_own_slot(self):
        registry = self.registry()
        self.reserve(registry, signed())
        self.reserve(registry, signed(product='f'*64))

    def test_reused_request_id_is_blocked_in_new_context(self):
        registry = self.registry()
        self.reserve(registry, signed(request_id='3'*32))
        with self.assertRaises(ReplayForbidden):
            self.reserve(registry, signed('action:2', request_id='3'*32))

    def test_restart_preserves_intent_without_terminal(self):
        self.reserve(self.registry(), signed())
        with self.assertRaises(ReplayForbidden):
            self.reserve(self.registry(), signed())

    def test_completed_and_unknown_events_never_release_reservation(self):
        for slot, available in [('action:1', True), ('action:2', False)]:
            registry = self.registry()
            row = self.reserve(registry, signed(slot))
            state = broker.LowerState(self.root/(slot.replace(':', '-')+'.json'))
            event = state.begin(identity.normalized_payload(PAYLOAD), time.monotonic()+1, row)
            state.update(event, state='terminal', model_response_available=available, usage_unknown=not available)
            with self.assertRaises(ReplayForbidden):
                self.reserve(self.registry(), signed(slot))

    def test_concurrent_processes_have_one_admission(self):
        self.registry()
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(process_reserve, [(str(self.root/'reserve'), signed()) for _ in range(8)]))
        self.assertEqual(results.count('admitted'), 1)
        self.assertEqual(results.count('blocked'), 7)

    def test_changed_key_or_history_cannot_reset_reserve(self):
        self.registry()
        with self.assertRaises(ValueError): RequestReserve(self.root/'reserve', b'x'*32)
        with self.assertRaises(ValueError): RequestReserve(self.root/'reserve', KEY, HISTORY)

    def test_seed_has_exact_six_unknown_and_known_subtotal(self):
        state = broker.LowerState(self.root/'stats.json', HISTORY)
        value = state.snapshot()
        self.assertEqual(value['attempts'], HISTORY['attempts'])
        self.assertEqual((value['runtime']['calls'], value['runtime']['successful_calls'], value['runtime']['usage_unknown_calls']), (16, 10, 6))
        self.assertEqual(value['historical_calls'], 16)
        self.assertEqual(value['current_invocation_calls'], 0)
        self.assertEqual(value['usage_accounting'], {'known_total_tokens': 84811, 'complete_total_tokens': None})
        from agentloop.lower_transport import snapshot_valid
        self.assertTrue(snapshot_valid(value))
        self.assertFalse(snapshot_valid(value, fresh=True))

    def test_all_sixteen_actual_old_request_ids_are_blocked(self):
        registry = self.registry(HISTORY)
        for old in HISTORY['attempts']:
            with self.assertRaises(ReplayForbidden):
                self.reserve(registry, signed(request_id=old['request_id']))

    def test_three_actual_old_products_remain_blocked(self):
        registry = self.registry(HISTORY)
        for product in HISTORY['blocked_products']:
            with self.assertRaises(ReplayForbidden):
                self.reserve(registry, signed(product=product))

    def test_historical_payload_deny_set_is_exact(self):
        registry = self.registry(HISTORY)
        self.assertEqual(registry.old_payloads, {row['payload_sha256'] for row in HISTORY['attempts']})
        self.assertEqual(len(registry.old_payloads), 16)
        # A known-byte synthetic historical payload verifies the actual hash
        # branch; no missing original request body is invented or sent.
        seed = copy.deepcopy(HISTORY)
        seed['attempts'][0]['payload_sha256'] = identity.digest(identity.normalized_payload(PAYLOAD, stream=False))
        seed['source_receipt']['mapping'][0]['payload_sha256'] = seed['attempts'][0]['payload_sha256']
        seed['source_receipt_sha256'] = identity.digest(identity.canonical(seed['source_receipt']))
        other = RequestReserve(self.root/'payload-reserve', KEY, seed)
        with self.assertRaises(ReplayForbidden): self.reserve(other, signed())

    def test_untrusted_mac_or_body_is_rejected(self):
        registry = self.registry()
        for header, body in [(None, PAYLOAD), (signed(key=b'x'*32), PAYLOAD), (signed(), {**PAYLOAD, 'input': 'changed'})]:
            with self.assertRaises((ValueError, TypeError)): self.reserve(registry, header, body)
        self.assertFalse(list((self.root/'reserve/contexts').iterdir()))

    def test_invalid_slot_fingerprint_or_case_is_rejected(self):
        for slot in ('action:0', 'action:11', 'accountability:0', 'accountability:11',
                     'action:', 'accountability', 'authoring:0', 'authoring:2', '/tmp/new-context'):
            with self.assertRaises(ValueError): signed(slot)
        value = json.loads(signed());value['identity']['request_fingerprint'] = '0'*64
        with self.assertRaises(ValueError): self.reserve(self.registry(), json.dumps(value))

    def test_reserve_rejects_parent_symlink_and_relative_root(self):
        (self.root/'real').mkdir()
        (self.root/'alias').symlink_to(self.root/'real', target_is_directory=True)
        with self.assertRaises(ValueError): RequestReserve(self.root/'alias/reserve', KEY)
        with self.assertRaises(ValueError): RequestReserve(Path('relative-reserve'), KEY)
        self.assertFalse((self.root/'real/reserve').exists())

    def test_history_requires_trusted_digest_and_strict_source_receipt(self):
        seed = self.root/'seed.json';seed.write_text(json.dumps(HISTORY))
        for bound in (None, '0'*64):
            with self.assertRaises(ValueError): load_history(seed, bound)
        for mutate in ('request_id', 'payload_sha256', 'source_receipt_sha256', 'product', 'mapping'):
            value = copy.deepcopy(HISTORY)
            if mutate in ('request_id', 'payload_sha256'): value['attempts'][0][mutate] = 'z'*64
            elif mutate == 'source_receipt_sha256': value[mutate] = '0'*64
            elif mutate == 'product': value['blocked_products'][0] = 'z'*64
            else: value['source_receipt']['mapping'].reverse()
            seed.write_text(json.dumps(value))
            with self.assertRaises(ValueError): load_history(seed, identity.digest(seed.read_bytes()))

    def test_bound_seed_cannot_hide_a_changed_source_artifact(self):
        value = copy.deepcopy(HISTORY)
        changed = self.root/'changed-source.json';changed.write_text('{}')
        value['source_receipt']['source_artifacts'][0]['path'] = str(changed)
        value['source_receipt_sha256'] = identity.digest(identity.canonical(value['source_receipt']))
        seed = self.root/'seed.json';seed.write_text(json.dumps(value))
        with self.assertRaises(ValueError): load_history(seed, identity.digest(seed.read_bytes()), verify_sources=True)

    def test_partial_unbound_reserve_cannot_generate_fresh_eligibility(self):
        path = self.root/'reserve';path.mkdir(mode=0o700)
        (path/'orphan-intent.json').write_text('{}')
        with self.assertRaises(ValueError): RequestReserve(path, KEY)

    def test_existing_stats_cannot_be_overwritten_or_claimed_as_broker_restart(self):
        stats = self.root/'stats.json'
        state = broker.LowerState(stats, HISTORY)
        before = stats.read_bytes()
        with self.assertRaises(FileExistsError): broker.LowerState(stats, HISTORY)
        self.assertEqual(before, stats.read_bytes())

    def binding(self):
        brokers = self.root/'run/brokers';evidence = brokers/'lower-lower-transport';evidence.mkdir(parents=True)
        path = identity.create_binding(brokers, evidence, 'http://127.0.0.1:9999/v1/responses')
        return brokers, path

    def test_binding_requires_registered_endpoint_mode_owner_and_scope(self):
        brokers, path = self.binding();endpoint = 'http://127.0.0.1:9999/v1/responses'
        self.assertEqual(identity.resolve_binding(brokers.parent, endpoint), path)
        with self.assertRaises(ValueError): identity.load_binding(path, endpoint, brokers=self.root/'candidate')
        with self.assertRaises((ValueError, OSError)): identity.load_binding(path, endpoint.replace('9999','9998'))
        path.chmod(0o644)
        with self.assertRaises(ValueError): identity.load_binding(path, endpoint)
        path.chmod(0o600)
        if os.geteuid() == 0:
            os.chown(path, 65534, -1)
            try:
                with self.assertRaises(ValueError): identity.load_binding(path, endpoint)
            finally: os.chown(path, 0, -1)
        with self.assertRaises(FileExistsError): identity.create_binding(brokers, path.parent/'lower-lower-transport', endpoint)

    def test_missing_or_symlink_binding_fails_closed(self):
        with self.assertRaises(FileNotFoundError): identity.resolve_binding(self.root/'run', 'http://127.0.0.1:9999/v1/responses')
        brokers, path = self.binding();alias = self.root/'alias';alias.symlink_to(brokers, target_is_directory=True)
        with self.assertRaises(ValueError): identity.load_binding(alias/path.name, 'http://127.0.0.1:9999/v1/responses')

    def test_effective_formal_pilot_smoke_controller_pass_only_owned_binding_path(self):
        brokers, binding = self.binding();endpoint = 'http://127.0.0.1:9999/v1/responses'
        for kind in ('formal','pilot','smoke'):
            run = brokers.parent
            ctl = Controller(ROOT, run/'lifecycle', endpoint, False, image='fixture-isolated-image', run_kind=kind)
            out = self.root/kind;out.mkdir()
            captured = []
            def fake_run(command, **kwargs):
                captured.append(command)
                (out/'launcher_result.json').write_text(json.dumps({'classification': 'launcher_infrastructure_error', 'dispatch_not_started': True, 'valid': False}))
                return SimpleNamespace(returncode=1, stdout='', stderr='')
            with patch('agentloop.two_round_controller.subprocess.run', side_effect=fake_run):
                ctl._run_case(self.root/'candidate', 'dev_001', out)
            self.assertEqual(len(captured), 1)
            argv = captured[0]
            self.assertEqual(argv[argv.index('--logical-context-binding')+1], str(binding))
            self.assertNotIn(identity.private_file(Path(json.loads(binding.read_bytes())['key_file'])).hex(), json.dumps(argv))

    def test_real_launcher_context_scoped_and_not_candidate_environment(self):
        _, binding = self.binding();endpoint = 'http://127.0.0.1:9999/v1/responses'
        repo = self.root/'candidate';repo.mkdir();(repo/'product.py').write_text('print(1)\n')
        case = self.root/'dev_001/case_input.json';case.parent.mkdir();case.write_text('{"case_id":"dev_001"}')
        seen = []
        def implementation(*args, **kwargs):
            context = identity.ACTIVE_CONTEXT.get();seen.append(dict(context))
            signed_header = identity.request_header(PAYLOAD, 'action:1')
            checked = identity.verify_request(signed_header, context['key'], identity.normalized_payload(PAYLOAD))
            self.assertEqual(checked['product_source_digest'], launcher.product_source_digest(repo))
            env = launcher.candidate_environment(repo, endpoint)
            self.assertNotIn(context['key'].hex(), json.dumps(env))
            self.assertNotIn('logical-context', json.dumps(env))
            return {'fixture': True}
        with patch.object(launcher, '_run_case_impl', side_effect=implementation):
            launcher.run_case(repo, case, self.root/'output', endpoint, 30, image='fixture-isolated-image', logical_context_binding=binding)
        self.assertEqual(len(seen), 1)
        self.assertIsNone(identity.ACTIVE_CONTEXT.get())
        with self.assertRaises(ValueError): identity.request_header(PAYLOAD, 'action:1')

    def test_candidate_docker_mounts_and_public_package_never_include_context_key(self):
        brokers, binding = self.binding()
        repo = self.root/'candidate';repo.mkdir()
        output = self.root/'output';output.mkdir()
        with patch.object(launcher, 'product_arguments', return_value=[]):
            command = launcher.docker_product_command(repo, output, {}, 'fixture', 'http://127.0.0.1:9999/v1/responses', formal.LOWER_IMAGE, None)
        self.assertEqual(command[command.index('--network')+1], 'none')
        mounted = [command[i+1] for i,v in enumerate(command[:-1]) if v == '-v']
        self.assertFalse(any(str(brokers) in m or 'logical-context' in m for m in mounted))
        self.assertNotIn('logical-context', json.dumps(command))
        public, manifest = formal.stage_public_package(self.root/'package')
        files = list(public.rglob('*'))
        self.assertTrue(files)
        self.assertFalse(any('logical-context' in p.name or 'request_reserve' in p.name for p in files))
        key = identity.private_file(Path(json.loads(binding.read_bytes())['key_file']))
        self.assertFalse(any(key in p.read_bytes() for p in files if p.is_file()))

    def test_public_and_hidden_endpoints_resolve_to_distinct_owned_bindings(self):
        brokers, public_binding = self.binding()
        hidden_evidence = brokers/'hidden_lower-lower-transport';hidden_evidence.mkdir()
        hidden_endpoint = 'http://127.0.0.1:9998/v1/responses'
        hidden_binding = identity.create_binding(brokers, hidden_evidence, hidden_endpoint)
        self.assertEqual(identity.resolve_binding(brokers.parent, hidden_endpoint), hidden_binding)
        self.assertNotEqual(hidden_binding, public_binding)
        _, key = identity.load_binding(hidden_binding, hidden_endpoint)
        header = identity.sign_request(key, PRODUCT, 'test_006', CASE, 'authoring:1', PAYLOAD)
        registry = RequestReserve(hidden_evidence/'request_reserve', key)
        self.assertEqual(self.reserve(registry, header)['case_id'], 'test_006')

    def test_no_host_candidate_or_unbound_real_launcher(self):
        with self.assertRaises(ValueError):
            launcher.run_case(self.root, self.root/'case.json', self.root/'out', 'http://127.0.0.1:9', 30)

    def test_actual_lower_http_gate_reaches_only_mock_worker_boundary(self):
        seed = self.root/'seed.json';seed.write_text(json.dumps(HISTORY))
        server = broker.LowerServer(('127.0.0.1', 0), endpoint='https://api.deepseek.com/v1/responses', key='fixture-not-real-provider-key',
            stats_path=self.root/'http/stats.json', context_key=KEY, history_seed=seed, history_seed_sha256=identity.digest(seed.read_bytes()))
        server.spawn_worker = Mock(side_effect=RuntimeError('EXPLICIT_ZERO_API_WORKER_BOUNDARY'))
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        def request(header):
            client = http.client.HTTPConnection(*server.server_address, timeout=5)
            raw = json.dumps(PAYLOAD, ensure_ascii=False).encode()
            headers = {'Authorization': 'Bearer broker-only-placeholder', 'Content-Type': 'application/json',
                broker.LOWER_DEADLINE_HEADER: str(time.monotonic()+10)}
            if header is not None: headers[identity.CONTEXT_HEADER] = header
            client.request('POST', '/v1/responses', body=raw, headers=headers)
            response = client.getresponse();result = (response.status, json.loads(response.read()));client.close();return result
        try:
            self.assertEqual(request(None)[0], 403)
            self.assertEqual(request(signed(key=b'x'*32))[0], 403)
            self.assertEqual(request(signed(request_id=HISTORY['attempts'][0]['request_id']))[0], 409)
            self.assertEqual(server.spawn_worker.call_count, 0)
            self.assertEqual(request(signed())[0], 598)
            self.assertEqual(server.spawn_worker.call_count, 1)
            self.assertEqual(request(signed())[0], 409)
            self.assertEqual(request(signed('action:2'))[0], 598)
            # D104.  The last action turn the loop can reach and the one
            # finish-accountability re-ask must pass the same signed gate and
            # reach the same worker boundary as any other logical request.
            self.assertEqual(request(signed(f'action:{launcher.MAX_ACTION_STEPS}'))[0], 598)
            self.assertEqual(request(signed('accountability:3'))[0], 598)
            self.assertEqual(request(signed('authoring:1'))[0], 598)
            self.assertEqual(server.spawn_worker.call_count, 5)
            value = server.state.snapshot()
            self.assertEqual(value['attempts'][:16], HISTORY['attempts'])
            self.assertEqual(value['current_invocation_calls'], 5)
            self.assertEqual(value['runtime']['upstream_attempts'], 16)
            self.assertEqual(value['attempts'][-1]['upstream_attempts'], 0)
            self.assertNotIn('"signature":', json.dumps(value))
            HTTP_EVIDENCE.update({'valid_mac_reached_mock_worker': True, 'mock_worker_calls': 5,
                'accepted_request_slots': ['action:1', 'action:2', f'action:{launcher.MAX_ACTION_STEPS}', 'accountability:3', 'authoring:1'],
                'historical_events_exact': True, 'actual_external_api_calls': 0, 'snapshot': value})
        finally:
            server.shutdown();server.stop_owned_workers();server.server_close();thread.join()

    def test_transport_worker_and_legacy_handler_are_byte_identical(self):
        before = (BASELINE/'evaluator/lower_responses_broker.py').read_text();after = (ROOT/'evaluator/lower_responses_broker.py').read_text()
        for name in ('lower_worker', 'Handler'):
            def part(text):
                return ast.get_source_segment(text, next(n for n in ast.parse(text).body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name))
            self.assertEqual(part(before), part(after))
        self.assertIn('AgentSWE-OpenClaw-Lower/3.0', before)


def historical_transport_suite():
    """Keep prior assertions untouched; supply the now-required trusted fixture context.

    These two source files explicitly use localhost synthetic HTTP providers.
    The adapter supplies evaluator fixture key/context/slot. Its two stall
    fixtures use an arrival handshake and 5s local deadline: paired baseline
    evidence showed that the old 0.4s fixture sometimes never dispatched.
    Production transport, 300/600s budgets, and original files are unchanged.
    """
    suite = unittest.TestSuite()
    for filename, class_name in [('test_v26_lower_transport.py', 'LowerTransportTests'),
                                 ('test_0910_lower_streaming.py', 'StreamingTests')]:
        spec = importlib.util.spec_from_file_location('historical_' + filename[:-3], ROOT/'evaluator/tests'/filename)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        base = getattr(module, class_name)
        def make_adapter(base, module):
            class TrustedFixture(base):
                def setUp(self):
                    server_class = module.broker.LowerServer
                    def server(*args, **kwargs):
                        kwargs['context_key'] = KEY
                        return server_class(*args, **kwargs)
                    with patch.object(module.broker, 'LowerServer', side_effect=server):
                        super().setUp()
                    self.trusted_token = identity.ACTIVE_CONTEXT.set({'key': KEY, 'product_source_digest': PRODUCT,
                        'case_id': 'dev_001', 'case_input_sha256': CASE})
                    original_model = module.launcher._request_model_json
                    self.model_patch = patch.object(module.launcher, '_request_model_json',
                        side_effect=lambda *a, **k: original_model(*a, request_slot='action:1', **k))
                    self.model_patch.start()
                    original_request = module.urllib.request.Request
                    def request(url, data=None, headers=None, *a, **k):
                        headers = dict(headers or {})
                        if url == self.endpoint and data is not None and not any(n.lower() == identity.CONTEXT_HEADER.lower() for n in headers):
                            headers[identity.CONTEXT_HEADER] = signed(payload=json.loads(data))
                        return original_request(url, data=data, headers=headers, *a, **k)
                    self.request_patch = patch.object(module.urllib.request, 'Request', side_effect=request)
                    self.request_patch.start()
                def tearDown(self):
                    self.request_patch.stop();self.model_patch.stop()
                    identity.ACTIVE_CONTEXT.reset(self.trusted_token)
                    super().tearDown()
            def confirmed_stall(self):
                arrival = threading.Event()
                observed = {}
                class Requests(list):
                    def append(items, value):
                        super().append(value)
                        observed['arrival_monotonic'] = time.monotonic()
                        arrival.set()
                self.provider.requests = Requests()
                class HoldUntilCleanup(threading.Event):
                    def wait(held, timeout=None):
                        return super().wait(10.0)
                self.provider.release = HoldUntilCleanup()
                started = time.monotonic()
                absolute_deadline = started + 5.0
                token = module.CASE_DEADLINE.set(absolute_deadline)
                context = contextvars.copy_context()
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(context.run, self.request, 'stall')
                        self.assertTrue(arrival.wait(3.0), 'fixture did not reach provider; no stream-stall claim')
                        self.assertLess(observed['arrival_monotonic'], absolute_deadline)
                        with self.assertRaises((module.urllib.error.URLError, TimeoutError, OSError)):
                            future.result(timeout=6.0)
                finally:
                    module.CASE_DEADLINE.reset(token)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 4.75)
                self.assertLess(elapsed, 6.5)
                self.assertEqual(len(self.provider.requests), 1)
                until = time.monotonic() + 1
                while self.server.state.snapshot()['runtime']['in_flight_calls'] and time.monotonic() < until:
                    time.sleep(.01)
                snapshot = self.server.state.snapshot()
                self.assertEqual(len(snapshot['attempts']), 1)
                event = snapshot['attempts'][0]
                self.assertEqual(event['deadline_monotonic'], absolute_deadline)
                self.assertEqual(event['upstream_attempts'], 1)
                self.assertTrue(event['usage_unknown'])
                self.assertTrue(event['worker_reaped'])
                self.assertEqual(event['state'], 'terminal')
                STALL_EVIDENCE.append({'test': self._testMethodName,
                    'arrival_after_seconds': observed['arrival_monotonic'] - started,
                    'elapsed_seconds': elapsed, 'fixture_absolute_budget_seconds': 5.0,
                    'actual_synthetic_dispatches': 1, 'no_retries': True, 'worker_reaped': True})
            for test_name in ('test_deadline_bounds_real_stalled_worker', 'test_absolute_case_deadline_still_bounds_stream'):
                if hasattr(base, test_name): setattr(TrustedFixture, test_name, confirmed_stall)
            TrustedFixture.__name__ = 'TrustedFixture_' + base.__name__
            return TrustedFixture
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(make_adapter(base, module)))
    return suite


def historical_product_suite():
    """Old product/public assertions with a real private binding for fake dispatch."""
    sys.path.insert(0, str(ROOT/'evaluator/tests'))
    product = importlib.import_module('test_v53_stable_product')
    public = importlib.import_module('test_public_package_isolation')
    class BoundReportReplay(product.ReportReplayTests):
        def setUp(self):
            super().setUp()
            brokers = self.root/'brokers';evidence = brokers/'lower-lower-transport'
            evidence.mkdir(parents=True)
            identity.create_binding(brokers, evidence, self.controller.broker_endpoint)
    return unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(product.StableProductTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(BoundReportReplay),
        unittest.defaultTestLoader.loadTestsFromTestCase(public.PublicPackageIsolationTest)])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history-seed', type=Path, required=True)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args();BASELINE=args.baseline_root;HISTORY=load_history(args.history_seed, identity.digest(args.history_seed.read_bytes()), verify_sources=True);HTTP_EVIDENCE={};STALL_EVIDENCE=[]
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(Tests), historical_transport_suite(), historical_product_suite()])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'valid': result.wasSuccessful(), 'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'historical_localhost_fixture_adapter': 'trusted key/context/slot plus two arrival-synchronized stall fixtures; original source files and production budgets/worker unchanged',
        'confirmed_localhost_stall_controls': STALL_EVIDENCE,
        'external_api_calls': 0, 'old_context_or_original_payload_bodies_invented': False, 'history_seed_sha256': identity.digest(args.history_seed.read_bytes()),
        'http_mock_boundary': HTTP_EVIDENCE, 'candidate_credentials_or_model_settings_changed': False,
        'old_request_ids': [x['request_id'] for x in HISTORY['attempts']]}
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2)+'\n')
    raise SystemExit(0 if report['valid'] else 1)
