"""Native boundary acceptance only: no Agent, oracle solution or model call."""
from __future__ import annotations
import base64
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from .connector_world import ConnectorWorld, FaultPlan, canonical, digest
from . import connector_world as module


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=3)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class ConnectorWorldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = None
        if os.environ.get('AGENTSWE_WORLD_TEST_OUTPUT'):
            self.root = Path(os.environ['AGENTSWE_WORLD_TEST_OUTPUT']) / self._testMethodName
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix='ocworld-test-')
            self.root = Path(self.temporary.name) / 'world'
        self.world = None
        self.callback_token = 'synthetic-callback-authority-1234567890'
        self.notifications = []

    def open_world(self, plan=FaultPlan(), seconds=10):
        self.world = ConnectorWorld(root=self.root, deadline=time.monotonic()+seconds, plan=plan,
            response_loss_observer=lambda role, event: self.notifications.append((role, event)))
        self.world.register_callback(self.callback_token)
        self.world.start()
        return self.world

    def tearDown(self):
        if self.world is not None:
            summary = self.world.close()
            self.assertTrue(summary['cleanup_complete'], summary)
            self.assertIsNone(summary['failure'], summary)
            self.assertEqual(summary['gateway_operations_performed_by_world'], 0)
        if self.temporary is not None:
            self.temporary.cleanup()

    def request(self, role, path, body, key, *, token=None):
        adapter = self.world.adapters[role]
        if token is None:
            token = getattr(adapter, 'token', '')
        c = UnixConnection(self.world.socket_path(role))
        try:
            c.request('POST', path, body=canonical(body), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                'Idempotency-Key': key})
            try:
                response = c.getresponse()
            except http.client.RemoteDisconnected:
                return None, None
            raw = response.read()
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            return response.status, value
        finally:
            c.close()

    def settled(self, count):
        end = time.monotonic()+2
        while time.monotonic() < end:
            if len(self.world.events) >= count and not self.world.active_requests:
                return
            time.sleep(.005)
        self.fail('native request evidence did not settle')

    def message(self, identity='dispatch-one', content=b'case-specific outcome'):
        return {'dispatch_id': identity, 'delivery_id': 'delivery-one',
            'route': {'provider': 'synthetic-channel', 'account_id': 'ops', 'peer_id': 'thread-17', 'destination_type': 'thread'},
            'intent': 'user_result', 'disclosure': 'full', 'media_type': 'text/plain',
            'content_base64': base64.b64encode(content).decode(), 'content_sha256': digest(content)}

    def receipt(self, message, accepted):
        value = {key: message[key] for key in ('dispatch_id', 'delivery_id')}
        value['provider_receipt_id'] = accepted['provider_receipt_id']
        if accepted.get('media_receipts'):
            value['media_receipts'] = accepted['media_receipts']
        return value

    def upload(self, body=b'abcde'*500, *, wrong_whole_hash=False):
        parts = [body[i:i+1024] for i in range(0, len(body), 1024)]
        chunks = [{'index': i, 'offset': i*1024, 'size': len(part), 'sha256': digest(part)} for i, part in enumerate(parts)]
        manifest = {'media_id': 'media-one', 'content_sha256': '0'*64 if wrong_whole_hash else digest(body),
            'size': len(body), 'media_type': 'application/octet-stream', 'chunk_size': 1024,
            'chunk_count': len(parts), 'chunks': chunks}
        payload = {'upload_id': 'upload:opaque:id', 'manifest_id': 'sha256:'+digest(canonical(manifest)), **manifest}
        return payload, parts

    def chunk(self, upload, init, index, content):
        return {'upload_id': upload['upload_id'], 'upload_session_id': init['upload_session_id'],
            'manifest_id': upload['manifest_id'], 'index': index, 'offset': index*1024,
            'size': len(content), 'chunk_sha256': digest(content), 'content_base64': base64.b64encode(content).decode()}

    def callback(self):
        body = b'approve case-specific result'
        return {'ingest_id': 'ingest-one', 'provider_event_id': 'event-one',
            'correlation_delivery_id': 'delivery-one', 'kind': 'button', 'action_id': 'approve',
            'route': self.message()['route'], 'content_base64': base64.b64encode(body).decode(),
            'content_sha256': digest(body), 'media_type': 'text/plain', 'destination_type': 'thread',
            'callback_token': self.callback_token, 'occurred_at_ms': 1_790_000_000_000}

    def test_four_explicit_unix_boundaries_have_no_implicit_tcp_fallback(self):
        world = self.open_world()
        self.assertEqual(set(world.adapters), {'effect', 'channel', 'media', 'interaction'})
        for role, adapter in world.adapters.items():
            self.assertEqual(adapter.server.address_family, socket.AF_UNIX)
            self.assertTrue(world.socket_path(role).is_socket())
        self.assertEqual(len({world.adapters[x].token for x in ('channel', 'media', 'interaction')}), 3)
        self.assertFalse(world.snapshot()['semantic_case_verified'])

    def test_acceptance_empty_identity_unknown_and_verified_are_distinct(self):
        self.open_world(FaultPlan(channel_receipt_schedule=('empty_platform_id', 'unknown', 'verified')))
        message = self.message()
        code, accepted = self.request('channel', '/v1/messages', message, message['dispatch_id'])
        self.assertEqual(code, 202); self.assertNotIn('platform_message_id', accepted)
        outcomes = []
        for _ in range(3):
            code, receipt = self.request('channel', '/v1/receipts', self.receipt(message, accepted), message['dispatch_id'])
            self.assertEqual(code, 200)
            outcomes.append((receipt['outcome'], bool(receipt['platform_message_id'])))
        self.assertEqual(outcomes, [('verified', False), ('unknown', False), ('verified', True)])
        self.settled(4)
        self.assertFalse(self.world.events[1]['response']['platform_message_id']['present'])
        self.assertTrue(self.world.events[3]['response']['platform_message_id']['present'])

    def test_channel_replay_is_stable_and_changed_body_conflicts(self):
        self.open_world(); message = self.message()
        _, first = self.request('channel', '/v1/messages', message, message['dispatch_id'])
        _, second = self.request('channel', '/v1/messages', message, message['dispatch_id'])
        self.assertFalse(first['duplicate']); self.assertTrue(second['duplicate'])
        self.assertEqual(first['provider_receipt_id'], second['provider_receipt_id'])
        changed = self.message(content=b'changed body')
        self.assertEqual(self.request('channel', '/v1/messages', changed, message['dispatch_id'])[0], 409)
        self.assertEqual(len(self.world.adapters['channel'].accepted), 1)

    def test_channel_loss_is_recorded_after_acceptance_before_fault_observer(self):
        self.open_world(FaultPlan(channel_drop_first_acceptance=True))
        message = self.message()
        self.assertEqual(self.request('channel', '/v1/messages', message, message['dispatch_id']), (None, None))
        self.settled(1)
        self.assertTrue(self.world.events[0]['response_lost_after_native_acceptance'])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.world.adapters['channel'].accepted), 1)
        self.assertEqual(json.loads((self.root/'events.jsonl').read_text())['sequence'], 1)
        self.assertTrue(self.request('channel', '/v1/messages', message, message['dispatch_id'])[1]['duplicate'])

    def test_media_chunk_response_loss_reconciles_offset_without_resending_bytes(self):
        self.open_world(FaultPlan(media_drop_stages=('chunk:0',)))
        upload, parts = self.upload(); uid = upload['upload_id']
        code, init = self.request('media', '/v1/uploads', upload, uid); self.assertEqual(code, 200)
        first = self.chunk(upload, init, 0, parts[0])
        self.assertEqual(self.request('media', '/v1/upload-chunks', first, uid+':chunk:0'), (None, None))
        code, observed = self.request('media', '/v1/upload-status',
            {'upload_id': uid, 'manifest_id': upload['manifest_id']}, uid+':status')
        self.assertEqual(observed['next_offset'], 1024)
        for index in range(1, len(parts)):
            self.assertEqual(self.request('media', '/v1/upload-chunks', self.chunk(upload, init, index, parts[index]), uid+f':chunk:{index}')[0], 200)
        final = {'upload_id': uid, 'upload_session_id': init['upload_session_id'],
            'manifest_id': upload['manifest_id'], 'chunk_count': len(parts)}
        self.assertTrue(self.request('media', '/v1/upload-finalize', final, uid+':finalize')[1]['complete'])
        provider = self.world.adapters['media']
        self.assertTrue(provider.exact_upload(uid, b''.join(parts)))
        self.assertEqual([x['stage'] for x in provider.requests].count('chunk:0'), 1)

    def test_media_init_and_finalize_loss_preserve_observable_provider_state(self):
        self.open_world(FaultPlan(media_drop_stages=('init', 'finalize')))
        upload, parts = self.upload(b'x'*1024); uid = upload['upload_id']
        self.assertEqual(self.request('media', '/v1/uploads', upload, uid), (None, None))
        status_body = {'upload_id': uid, 'manifest_id': upload['manifest_id']}
        _, status = self.request('media', '/v1/upload-status', status_body, uid+':status')
        self.assertEqual(status['state'], 'initialized')
        self.assertEqual(self.request('media', '/v1/upload-chunks', self.chunk(upload, status, 0, parts[0]), uid+':chunk:0')[0], 200)
        final = {'upload_id': uid, 'upload_session_id': status['upload_session_id'],
            'manifest_id': upload['manifest_id'], 'chunk_count': 1}
        self.assertEqual(self.request('media', '/v1/upload-finalize', final, uid+':finalize'), (None, None))
        _, status = self.request('media', '/v1/upload-status', status_body, uid+':status')
        self.assertEqual(status['state'], 'uploaded'); self.assertTrue(status['provider_media_id'])

    def test_media_whole_hash_mismatch_cannot_finalize(self):
        self.open_world(); upload, parts = self.upload(b'x'*1024, wrong_whole_hash=True); uid = upload['upload_id']
        _, init = self.request('media', '/v1/uploads', upload, uid)
        self.assertEqual(self.request('media', '/v1/upload-chunks', self.chunk(upload, init, 0, parts[0]), uid+':chunk:0')[0], 200)
        final = {'upload_id': uid, 'upload_session_id': init['upload_session_id'], 'manifest_id': upload['manifest_id'], 'chunk_count': 1}
        self.assertEqual(self.request('media', '/v1/upload-finalize', final, uid+':finalize')[0], 409)
        self.assertFalse(self.world.adapters['media'].uploads[uid]['finalized'])

    def test_media_receipts_are_partial_then_complete_as_an_unordered_set(self):
        self.open_world(FaultPlan(channel_partial_media_first=True))
        message = self.message()
        message['media'] = [{'media_id': f'm{i}', 'manifest_id': f'manifest{i}', 'provider_media_id': f'provider{i}',
            'media_type': 'application/octet-stream', 'size': 1024, 'content_sha256': digest(bytes([i])), 'ordinal': i} for i in range(2)]
        _, accepted = self.request('channel', '/v1/messages', message, message['dispatch_id'])
        _, partial = self.request('channel', '/v1/receipts', self.receipt(message, accepted), message['dispatch_id'])
        _, complete = self.request('channel', '/v1/receipts', self.receipt(message, accepted), message['dispatch_id'])
        self.assertEqual(partial['outcome'], 'unknown'); self.assertEqual(len(partial['media_receipts']), 1)
        self.assertEqual(complete['outcome'], 'verified')
        self.assertEqual([x['media_id'] for x in complete['media_receipts']], ['m1', 'm0'])

    def test_interaction_loss_reconcile_replay_and_changed_event_conflict(self):
        self.open_world(FaultPlan(interaction_drop_first_acceptance=True))
        body = self.callback(); iid = body['ingest_id']
        self.assertEqual(self.request('interaction', '/v1/interactions/verify', body, iid), (None, None))
        _, status = self.request('interaction', '/v1/interactions/status',
            {'ingest_id': iid, 'provider_event_id': body['provider_event_id']}, iid+':status')
        self.assertEqual(status['state'], 'accepted')
        self.assertTrue(self.request('interaction', '/v1/interactions/verify', body, iid)[1]['duplicate'])
        self.assertEqual(self.request('interaction', '/v1/interactions/verify', {**body, 'action_id': 'changed'}, iid)[0], 409)
        self.assertEqual(len(self.world.adapters['interaction'].accepted), 1)

    def test_effect_loss_replay_and_changed_payload_conflict(self):
        self.open_world(FaultPlan(effect_drop_first_acceptance=True))
        body = {'task_id': 'task', 'effect_id': 'effect', 'kind': 'notify', 'payload': {'value': 1}}
        self.assertEqual(self.request('effect', '/effects', body, 'effect'), (None, None))
        self.assertTrue(self.request('effect', '/effects', body, 'effect')[1]['duplicate'])
        self.assertEqual(self.request('effect', '/effects', {**body, 'payload': {'value': 2}}, 'effect')[0], 409)
        self.assertEqual(len(self.world.adapters['effect'].accepted), 1)

    def test_cross_service_credentials_and_arbitrary_routes_are_rejected(self):
        self.open_world(); message = self.message()
        self.assertEqual(self.request('channel', '/v1/messages', message, message['dispatch_id'], token=self.world.adapters['media'].token)[0], 400)
        self.assertEqual(self.request('channel', '/v1/responses', message, message['dispatch_id'])[0], 404)
        self.assertEqual(self.request('interaction', '/v1/interactions/verify', {**self.callback(), 'callback_token': 'forged'}, 'ingest-one')[0], 403)
        self.assertFalse(self.world.adapters['channel'].accepted)
        self.assertFalse(self.world.adapters['interaction'].accepted)

    def test_evidence_redacts_authority_route_and_body_without_losing_identity_presence(self):
        self.open_world(); body = self.callback()
        self.request('interaction', '/v1/interactions/verify', body, body['ingest_id']); self.settled(1)
        serialized = (self.root/'events.jsonl').read_text()
        for value in [self.callback_token, body['content_base64'], *self.world.private_values]:
            self.assertNotIn(value, serialized)
        event = self.world.events[0]
        self.assertEqual(event['request']['content_base64']['sha256'], digest(canonical(body['content_base64'])))
        self.assertTrue(event['response']['provider_interaction_id']['present'])
        self.assertTrue(event['request']['route']['redacted'])
        self.assertTrue(event['service_authorization_valid'])

    def test_absolute_deadline_disconnects_an_unfinished_header(self):
        self.open_world(seconds=.35)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(2)
        try:
            sock.connect(str(self.world.socket_path('channel')))
            sock.sendall(b'POST /v1/messages HTTP/1.1\r\nX-Trickle: ')
            end = time.monotonic()+.8
            while time.monotonic() < end:
                try:
                    sock.sendall(b'x')
                except OSError:
                    break
                time.sleep(.03)
            self.assertTrue(self.world.deadline_reached)
            try:
                self.assertEqual(sock.recv(1024), b'')
            except ConnectionResetError:
                pass
        finally:
            sock.close()
        self.assertFalse(self.world.adapters['channel'].accepted)

    def test_malformed_native_requests_are_rejected_without_a_service_crash(self):
        self.open_world()
        upload, _ = self.upload(b'x'*1024)
        mutations = [{'chunk_size': '1024'}, {'size': []}, {'chunk_count': True},
                     {'chunks': [None]}, {'upload_id': ['not', 'an', 'id']}]
        for mutation in mutations:
            code, _ = self.request('media', '/v1/uploads', {**upload, **mutation}, upload['upload_id'])
            self.assertIn(code, (400, 409), mutation)
        self.assertEqual(self.request('channel', '/v1/messages', {**self.message(), 'dispatch_id': []}, 'wrong')[0], 400)
        callback = {**self.callback(), 'content_base64': '%%%'}
        self.assertEqual(self.request('interaction', '/v1/interactions/verify', callback, callback['ingest_id'])[0], 400)
        self.settled(7)
        self.assertIsNone(self.world.failure)
        self.assertFalse(self.world.adapters['media'].uploads)
        self.assertFalse(self.world.adapters['channel'].accepted)

    def test_evidence_redacts_private_values_in_arbitrary_keys_and_paths(self):
        self.open_world()
        secret = self.world.adapters['channel'].token
        body = {**self.message(), secret: secret}
        self.request('channel', '/'+secret, body, 'wrong'); self.settled(1)
        self.assertNotIn(secret, (self.root/'events.jsonl').read_text())
        self.assertEqual(self.world.events[0]['http_status'], 404)

    def test_existing_evidence_root_is_not_overwritten(self):
        self.open_world()
        with self.assertRaises(FileExistsError):
            ConnectorWorld(root=self.root, deadline=time.monotonic()+1)

    def test_constructor_failure_cleans_owned_sockets_and_close_is_idempotent(self):
        socket_root = Path(tempfile.mkdtemp(prefix='ocw-construction-test-'))
        original = module.UnixHTTPServer
        calls = []
        def failing_factory(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('synthetic second-listener failure')
            return original(*args, **kwargs)
        failed_root = self.root.parent / (self.root.name+'-failed-construction')
        with patch.object(module.tempfile, 'mkdtemp', return_value=str(socket_root)), \
             patch.object(module, 'UnixHTTPServer', side_effect=failing_factory):
            with self.assertRaises(OSError):
                ConnectorWorld(root=failed_root, deadline=time.monotonic()+5)
        self.assertFalse(socket_root.exists())
        self.open_world()
        first = self.world.close()
        self.assertTrue(first['cleanup_complete'])
        self.assertEqual(first, self.world.close())


if __name__ == '__main__':
    unittest.main()
