"""Real Unix socket projections and digest queries; synthetic evaluation state."""
import hashlib
import importlib.util
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = next(path for path in Path(__file__).resolve().parents if (path/'harbor/formal_one_stop.py').is_file())
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'harbor'))
spec=importlib.util.spec_from_file_location('builder_boundary_0910',ROOT/'harbor/formal_one_stop.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
from public_feedback import public_payload, semantic_feedback


class BuilderBoundaryTests(unittest.TestCase):
    def test_feedback_contract_projection_retains_semantics_only(self):
        contract={'assessment':'Real public-task feedback', 'major_errors':['Missing transaction receipt'],
                  'dimensions':{'task_completion':{'score':12,'max':60,'rationale':'incomplete'}},
                  'oracle_summary':'PRIVATE_ORACLE_SENTINEL', 'judge_prompt':'PRIVATE_PROMPT_SENTINEL',
                  'raw_response_path':'@@AGENTSWE_LEGACY_DATA@@/private-request.json'}
        projected=semantic_feedback(contract)
        self.assertEqual(projected['assessment'],contract['assessment'])
        self.assertEqual(projected['dimensions']['task_completion']['score'],12)
        self.assertNotIn('PRIVATE_',json.dumps(projected))
        self.assertEqual(public_payload(projected),projected)

    def lifecycle(self,root):
        workspace=root/'workspace';workspace.mkdir();(workspace/'solution.patch').write_text('delivery')
        if hasattr(runner,'DevController'):
            lifecycle=runner.DevController(run_dir=root,workspace=workspace,evaluate=Mock(side_effect=AssertionError('must not evaluate duplicate')))
            digest=runner.tree_digest(workspace)
            record={'submission_id':'candidate-001','submission_number':1,'candidate_digest':digest,'state':'completed',
                    'feedback_text':'Authoritative public feedback', 'feedback_digest':'f'*64,
                    'results':{'private_path':'@@AGENTSWE_LEGACY_DATA@@/PRIVATE_PATH_SENTINEL'}, 'oracle':'PRIVATE_ORACLE_SENTINEL'}
            lifecycle.records=[record];lifecycle.by_id={'candidate-001':record};lifecycle.by_digest={digest:record}
            lifecycle.frozen={'candidate_digest':digest,'path':'@@AGENTSWE_LEGACY_DATA@@/PRIVATE_PATH_SENTINEL'}
            start=lifecycle.start;stop=lifecycle.stop
            action={'action':'status','submission_id':'candidate-001'}
        else:
            lifecycle=object.__new__(runner.BuilderLifecycle)
            lifecycle.run_dir=root;lifecycle.workspace=workspace;lifecycle.session_id='synthetic-session'
            lifecycle.connection_id='synthetic-channel';lifecycle.lock=threading.RLock()
            lifecycle.token='synthetic-token';lifecycle.socket_path=root/'socket';lifecycle.server=None
            lifecycle.feedback_path=None;lifecycle.feedback_digest=None
            if 'claude' in str(ROOT):
                digest=runner.candidate_tree_digest(workspace)
                record={'submission_number':1,'candidate_digest':'c'*64,'delivery_digest':digest,'state':'completed',
                        'delivery_path':'@@AGENTSWE_LEGACY_DATA@@/PRIVATE_PATH_SENTINEL','result_judge_contract':{'oracle':'PRIVATE_ORACLE_SENTINEL'}}
                lifecycle.controller=SimpleNamespace(rounds=[record],frozen={'path':'@@AGENTSWE_LEGACY_DATA@@/PRIVATE_PATH_SENTINEL'})
                lifecycle._preflight=Mock(side_effect=AssertionError('must not preflight duplicate'))
                start=lambda:runner.start_submission_server(lifecycle)
            else:
                digest=runner.digest(workspace)
                record={'submission_number':1,'candidate_digest':digest,'state':'completed','accepted':True,
                        'result_judge_contract':{'oracle':'PRIVATE_ORACLE_SENTINEL'},'result_path':'@@AGENTSWE_LEGACY_DATA@@/PRIVATE_PATH_SENTINEL'}
                lifecycle.controller=SimpleNamespace(records=[record],feedback={'score':45,'judge_prompt':'PRIVATE_PROMPT_SENTINEL'},
                    frozen={'path':'@@AGENTSWE_LEGACY_DATA@@/PRIVATE_PATH_SENTINEL'})
                lifecycle._validate_workspace=Mock(side_effect=AssertionError('must not preflight duplicate'))
                start=lambda:runner.start_builder_server(lifecycle)
            stop=lifecycle.close;action={'action':'status'}
        return lifecycle,start,stop,action

    def test_same_digest_after_freeze_is_query_without_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            lifecycle,_,_,_=self.lifecycle(Path(directory))
            code,payload=lifecycle.submit(None)
            self.assertEqual(code,200)
            self.assertTrue(payload['duplicate'])

    def test_actual_socket_never_serializes_private_record_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            lifecycle,start,stop,action=self.lifecycle(Path(directory))
            start()
            try:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.connect(str(lifecycle.socket_path))
                    client.sendall(json.dumps({**action,'token':lifecycle.token}).encode()+b'\n')
                    answer=client.makefile('rb').readline().decode()
                self.assertEqual(json.loads(answer)['status'],200)
                for marker in ('PRIVATE_ORACLE_SENTINEL','PRIVATE_PROMPT_SENTINEL','PRIVATE_PATH_SENTINEL','@@AGENTSWE_LEGACY_DATA@@'):
                    self.assertNotIn(marker,answer)
            finally:
                stop()


if __name__=='__main__': unittest.main()
