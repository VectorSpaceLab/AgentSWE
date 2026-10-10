"""Runner contract tests; mocks are never evidence of model task success."""
import subprocess
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evaluator.formal_gates import validate_case_record
from lower_agent.embedded_agent import EmbeddedAgent, command
from lower_agent.entry_contract import PRODUCTION_ENTRY
from lower_agent.gateway_cluster import GatewayCluster
from lower_agent.launcher import write_config


class EmbeddedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.cluster=SimpleNamespace(output=self.root/'output',workspace=self.root/'workspace',
            product=self.root/'product',state=self.root/'state',runtime={},env={},
            endpoint='http://127.0.0.1:9/v1/responses',bridge=19876,case_socket=self.root/'case.sock',
            node=Path('/pinned/bin/node'),deadline=time.monotonic()+30,product_readonly=True,
            sandboxes={'A':SimpleNamespace(namespace={'net_inode':1}),
                       'B':SimpleNamespace(namespace={'net_inode':2})})
        self.cluster.output.mkdir();self.cluster.workspace.mkdir()
        self.process=Mock();self.process.pid=999;self.process.wait.return_value=0
        self.sandbox=Mock(namespace={'net_inode':3},native_relays={})
        self.sandbox.start.return_value=self.process
        self.sandbox.close.return_value={'valid':True,'gateway_wrapper_reaped':True}
        self.factory=patch('lower_agent.embedded_agent.ProductSandbox',return_value=self.sandbox)
        self.mock=self.factory.start()
    def tearDown(self):self.factory.stop();self.temp.cleanup()
    def create(self):return EmbeddedAgent(self.cluster,message='A real task, not a recovery script.',
                                         session_key='agent:main:case-dynamic')
    def test_native_entry_has_fixed_effort_case_session_and_no_delivery_or_fallback(self):
        agent=self.create();args=agent.argv
        self.assertEqual(args[2:5],['agent','--local','--agent'])
        self.assertEqual(args[args.index('--thinking')+1],'medium')
        self.assertEqual(args[args.index('--session-key')+1],'agent:main:case-dynamic')
        self.assertLessEqual(int(args[args.index('--timeout')+1]),30)
        for forbidden in ('--fallback','--deliver','--model','--provider'):
            self.assertNotIn(forbidden,args)
        self.assertEqual(self.mock.call_args.kwargs['case_socket'],self.cluster.case_socket)
        self.assertNotIn('fixture_services',self.mock.call_args.kwargs)
        self.assertNotIn('gateway_id',self.mock.call_args.kwargs)
        self.assertNotIn('native_origin_credential',self.mock.call_args.kwargs)
        self.assertFalse((self.cluster.workspace/'agent_result.json').exists())
    def test_one_native_process_and_verified_cleanup(self):
        agent=self.create();agent.start()
        with self.assertRaises(RuntimeError):agent.start()
        self.assertEqual(agent.wait()['status'],'completed')
        evidence=agent.close()
        self.assertTrue(evidence['cleanup_complete'])
        self.assertEqual(evidence['production_entry'],PRODUCTION_ENTRY)
        self.assertFalse(evidence['fallback_attempted'])
        self.assertEqual(self.sandbox.start.call_count,1)
    def test_reasoning_cannot_share_either_fault_namespace(self):
        for inode in (1,2):
            with self.subTest(inode=inode):
                self.sandbox.namespace={'net_inode':inode}
                # Separate attempt dirs; neither reuses a case task file.
                with tempfile.TemporaryDirectory(dir=self.root) as path:
                    self.cluster.output=Path(path)/'out';self.cluster.output.mkdir()
                    self.cluster.workspace=Path(path)/'work';self.cluster.workspace.mkdir()
                    with self.assertRaises(RuntimeError):self.create().start()
    def test_timeout_does_not_start_a_second_model_run(self):
        agent=self.create();agent.start()
        self.process.wait.side_effect=subprocess.TimeoutExpired('native',1)
        self.assertEqual(agent.wait()['status'],'timeout')
        agent.close();self.assertEqual(self.sandbox.start.call_count,1)
    def test_task_file_cannot_overwrite_candidate_output(self):
        path=self.cluster.workspace/'agentswe-native-task.md';path.write_text('existing')
        with self.assertRaises(FileExistsError):self.create()
        self.assertEqual(path.read_text(),'existing')
    def test_expired_budget_cannot_be_renewed(self):
        with self.assertRaises(TimeoutError):
            command(self.cluster,Path('task'),'agent:main:case',time.monotonic()-1)
    def test_historical_gateway_entry_is_not_relabelled_as_embedded(self):
        errors=validate_case_record({'case_id':'test_001','production_entry':
            'node openclaw.mjs gateway -> gateway call agent -> agent.wait'},'test_001')
        self.assertTrue(any('entry is not attested' in error for error in errors))
    def test_expected_restart_is_observable_without_automatic_action_retry(self):
        cluster=GatewayCluster.__new__(GatewayCluster)
        cluster.ids={'A':'A','B':'B'};cluster.closed=False
        cluster.lifecycle_lock=threading.RLock();cluster.restarting={'B'}
        cluster.sandboxes={};cluster.native=Mock()
        result=cluster.rpc('B','handoff.inspect',{'exact':'agent-chosen'})
        self.assertEqual(result['error'],'selected_gateway_restarting')
        self.assertFalse(result['dispatched']);cluster.native.deadline_rpc.assert_not_called()
    def test_non_case_background_schedulers_are_disabled_without_model_or_budget_change(self):
        path=write_config(self.cluster.state,self.cluster.workspace,
                          'http://127.0.0.1:19876/v1/responses','synthetic-gateway-token')
        value=json.loads(path.read_text())
        self.assertEqual(value['agents']['defaults']['heartbeat']['every'],'0m')
        self.assertIs(value['cron']['enabled'],False)
        self.assertEqual(value['agents']['defaults']['timeoutSeconds'],600)
        self.assertEqual(value['agents']['defaults']['model']['primary'],'broker/deepseek-flash')


if __name__=='__main__':unittest.main()
