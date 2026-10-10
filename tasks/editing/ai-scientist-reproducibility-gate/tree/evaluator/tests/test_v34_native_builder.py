"""Native evidence boundaries using explicitly synthetic Codex-shaped records.

No model or product result is claimed by these fixtures. A separate pinned
binary integration probe exercises the actual Codex/Harbor producer.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harbor import native_builder_evidence as native


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


class NativeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.thread = '11111111-1111-4111-8111-111111111111'
        (self.root / 'builder_job_config.json').write_text(json.dumps({'job_name':'builder', 'jobs_dir':str(self.root/'jobs')}))
        self.stream = self.root/'jobs/builder/trial/agent/codex.txt'
        self.rollout = self.stream.parent/'sessions/2026/09/10/rollout-unit.jsonl'
        self.stream_rows = [{'type':'thread.started','thread_id':self.thread}, {'type':'turn.started'}]
        jsonl(self.stream, self.stream_rows)
        self.observations = [native.observe_thread(self.root, [])]
        self.rows = [
            {'timestamp':'2026-09-15T00:00:00Z','type':'session_meta','payload':{'id':self.thread, 'cli_version':'0.144.1','source':'exec','cwd':'/workspace'}},
            {'timestamp':'2026-09-15T00:00:01Z','type':'turn_context','payload':{'model':'gpt-5.6-sol','effort':'max'}}]
        self.records = []; self.deliveries = []
        for number in (1, 2):
            previous = self.records[-1]['feedback_digest'] if self.records else None
            feedback = {'public_cases':{'dev_001':{'semantic_feedback':'synthetic A'}, 'dev_002':{'semantic_feedback':'synthetic B'}}, 'dev_scores':{'dev_001':40,'dev_002':30}}
            path = self.root/f'lifecycle/feedback/candidate_{number:03d}.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(feedback, indent=2)+'\n')
            digest = native.sha(path.read_bytes())
            record = {'submission_number':number, 'builder_session_id':self.thread,
                      'candidate_digest':str(number)*64, 'feedback_digest':digest,
                      'feedback_path':str(path),'feedback_digest_ack':previous,
                      'build':{'candidate_repo_digest':str(number)*64,'product_source_digest':str(number)*64}}
            self.records.append(record)
            self.deliveries.append({'candidate_number':number,'builder_session_id':self.thread,'feedback_digest':digest})
            payload = {k:v for k,v in record.items() if k not in ('feedback_path','build')}
            payload['feedback'] = feedback
            self.deliveries[-1]['payload_sha256'] = native.sha(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
            self.rows.extend([
                {'timestamp':f'2026-09-15T00:00:{number * 2:02d}Z','type':'response_item','payload':{'type':'custom_tool_call','call_id':f'call{number}', 'input':f'submit_dev_candidate --wait --feedback-digest {previous}'}},
                {'timestamp':f'2026-09-15T00:00:{number * 2 + 1:02d}Z','type':'response_item','payload':{'type':'custom_tool_call_output','call_id':f'call{number}',
                                                 'output':[{'type':'input_text','text':'Script completed\nOutput:\n'+json.dumps({'status':200,'payload':payload}, indent=2)}]}}])
        self.stream_rows.append({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}})
        self.save()

    def save(self):
        jsonl(self.stream, self.stream_rows); jsonl(self.rollout, self.rows)

    def proof(self, **kwargs):
        self.save()
        return native.verify_native(self.root,self.records,self.deliveries,self.observations,**kwargs)

    def rejected(self):
        value=self.proof(); self.assertFalse(value['valid'],value); self.assertTrue(value['errors'])

    def test_full_native_feedback_and_distinct_revision(self):
        value=self.proof(); self.assertTrue(value['valid'],value)
        self.assertTrue(value['revision_observed']); self.assertEqual(len(value['feedback_received']),2)

    def test_timestamp_first_rollout_envelopes_are_parsed(self):
        self.save()
        parsed = native.events(self.rollout.read_bytes(), rollout=True)
        self.assertEqual(parsed[0]['type'], 'session_meta')
        self.assertEqual(parsed[0]['payload']['id'], self.thread)
        self.assertTrue(self.proof()['valid'])

    def test_one_submission_is_valid_without_forced_revision(self):
        self.records=self.records[:1]; self.rows=self.rows[:4]
        value=self.proof(); self.assertTrue(value['valid'],value); self.assertFalse(value['revision_observed'])

    def test_missing_native_files_cannot_be_self_attested(self):
        self.stream.unlink()
        value=native.verify_native(self.root,self.records,self.deliveries,self.observations)
        self.assertFalse(value['valid'])

    def test_prefix_replacement_is_rejected(self):
        self.stream_rows[0]['thread_id']='22222222-2222-4222-8222-222222222222'; self.rejected()

    def test_second_thread_is_rejected(self):
        self.stream_rows.insert(2,copy.deepcopy(self.stream_rows[0])); self.rejected()

    def test_foreign_rollout_is_rejected(self):
        self.rows[0]['payload']['id']='22222222-2222-4222-8222-222222222222'
        value = self.proof()
        self.assertFalse(value['valid'])
        self.assertEqual(value['errors'], ['rollout identity disagrees with thread.started'])

    def test_malformed_rollout_envelopes_are_rejected(self):
        malformed = (
            b'{"timestamp":"2026-09-15T00:00:00Z","type":"session_meta"\n',
            b'{"timestamp":"2026-09-15T00:00:00Z"}\n',
            b'["timestamp","2026-09-15T00:00:00Z"]\n',
        )
        for data in malformed:
            with self.subTest(data=data), self.assertRaises(native.NativeEvidenceError):
                native.events(data, rollout=True)

    def test_malformed_rollout_file_fails_closed_in_verifier(self):
        self.save()
        self.rollout.write_text('{"timestamp":"2026-09-15T00:00:00Z"}\n')
        value = native.verify_native(
            self.root, self.records, self.deliveries, self.observations)
        self.assertFalse(value['valid'])
        self.assertEqual(value['errors'], ['native JSON event lacks a string type'])

    def test_verifier_never_reads_or_replays_historical_provider_request(self):
        historical = self.root.parent / (self.root.name + '-historical')
        historical.mkdir()
        self.addCleanup(shutil.rmtree, historical, True)
        request = historical / 'provider-request.json'
        request.write_text('{"sealed":"do-not-read-or-replay"}\n')
        original_read_bytes = Path.read_bytes
        reads = []

        def observed_read(path):
            resolved = Path(path).resolve()
            if resolved == request.resolve():
                raise AssertionError('historical provider request was read')
            reads.append(resolved)
            return original_read_bytes(path)

        with mock.patch.object(Path, 'read_bytes', observed_read), \
                mock.patch('socket.create_connection', side_effect=AssertionError('network attempted')), \
                mock.patch('urllib.request.urlopen', side_effect=AssertionError('provider replay attempted')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('process replay attempted')), \
                mock.patch('subprocess.run', side_effect=AssertionError('process replay attempted')):
            value = self.proof()
        self.assertTrue(value['valid'], value)
        self.assertNotIn(request.resolve(), reads)
        self.assertTrue(all(path.is_relative_to(self.root.resolve()) for path in reads))
        request.unlink()

    def test_historical_rollout_in_current_session_tree_is_not_selected(self):
        historical = self.stream.parent / 'sessions/2026/09/14/rollout-old.jsonl'
        jsonl(historical, self.rows)
        value = self.proof()
        self.assertFalse(value['valid'])
        self.assertEqual(value['errors'], ['expected exactly one native rollout'])

    def test_wrong_cli_version_is_rejected(self):
        self.rows[0]['payload']['cli_version']='0.100.0'; self.rejected()

    def test_wrong_model_or_effort_is_rejected(self):
        self.rows[1]['payload']['effort']='high'; self.rejected()

    def test_foreign_workspace_is_rejected(self):
        self.rows[0]['payload']['cwd']='/other'; self.rejected()

    def test_socket_receipt_is_required(self):
        self.deliveries=[]; self.rejected()

    def test_incomplete_feedback_text_is_rejected(self):
        self.rows[3]['payload']['output'][0]['text']=self.records[0]['feedback_digest']; self.rejected()

    def test_changed_authoritative_feedback_is_rejected(self):
        Path(self.records[0]['feedback_path']).write_text('{}\n'); self.rejected()

    def test_no_post_feedback_model_action_is_rejected(self):
        self.rows[4]['payload']['input']='unrelated'; self.rejected()

    def test_wrong_revision_ack_is_rejected(self):
        self.records[1]['feedback_digest_ack']='f'*64; self.rejected()

    def test_same_product_is_not_a_revision(self):
        self.records[1]['build']=self.records[0]['build']; self.rejected()

    def test_different_git_metadata_cannot_prove_source_revision(self):
        self.records[1]['build']['product_source_digest']=self.records[0]['build']['product_source_digest']
        self.assertNotEqual(self.records[1]['build']['candidate_repo_digest'],self.records[0]['build']['candidate_repo_digest'])
        self.rejected()

    def test_missing_stable_digest_cannot_fall_back_to_git_identity(self):
        self.records[1]['build'].pop('product_source_digest')
        self.rejected()

    def test_interruption_not_normal_exit(self):
        self.stream_rows.pop(); self.rejected()
        self.assertTrue(self.proof(allow_interrupted=True)['valid'])

    def test_partial_final_rollout_is_rejected(self):
        self.rollout.write_bytes(self.rollout.read_bytes().rstrip(b'\n'))
        self.assertFalse(native.verify_native(self.root,self.records,self.deliveries,self.observations)['valid'])

    def test_symlinked_rollout_is_rejected(self):
        target=self.root/'replacement'; target.write_bytes(self.rollout.read_bytes())
        self.rollout.unlink(); self.rollout.symlink_to(target)
        self.assertFalse(native.verify_native(self.root,self.records,self.deliveries,self.observations)['valid'])

    def test_ambiguous_trial_is_rejected(self):
        jsonl(self.root/'jobs/builder/other/agent/codex.txt',self.stream_rows); self.rejected()


if __name__=='__main__': unittest.main()
