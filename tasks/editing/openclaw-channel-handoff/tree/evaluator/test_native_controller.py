"""Actual Unix socket/durable state regressions; synthetic semantic fixtures."""
import json, socket, tempfile, time, unittest
from pathlib import Path
from controller.builder_session_controller import BuilderSessionController
from controller.two_round_controller import DEV, tree_digest
from evaluator.test_agentloop_invariants import AgentLoopInvariantTests
from evaluator.test_dev_feedback import semantic_fixture
from harbor.native_builder_evidence import classify_native_termination

class ControllerTests(unittest.TestCase):
 def setup_controller(self,root,evaluate):
  source=root/'source';source.mkdir();workspace=root/'workspace'
  AgentLoopInvariantTests()._write_delivery(workspace,'one')
  return BuilderSessionController(run_dir=root/'run',workspace=workspace,source=source,evaluate=evaluate,require_native_evidence=False)
 def write_delivery(self,c,name):AgentLoopInvariantTests()._write_delivery(c.workspace,name)
 def call(self,c,**args):
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
   s.settimeout(10);s.connect(str(c.socket_path));s.sendall((json.dumps({'token':c.token,'builder_session_id':c.session_id,**args})+'\n').encode());return json.loads(s.makefile().readline())
 def test_unknown_product_and_reports_cannot_resample_or_restart(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);calls=[];c=None
   def evaluate(n,p):
    ledger=json.loads(c.intent_path.read_text());self.assertEqual(len(ledger),1)
    reservation=next(iter(ledger.values()));self.assertEqual(reservation['state'],'unknown');self.assertEqual(reservation['product_digest'],tree_digest(p))
    calls.append((n,tree_digest(p)));return {case:{'classification':'infrastructure-invalid'} for case in DEV}
   c=self.setup_controller(root,evaluate)
   code,first=c.submit(c.session_id);self.assertEqual(code,503);self.assertTrue(first['resampling_forbidden']);self.assertEqual(len(calls),1)
   self.assertEqual(c.submit(c.session_id),(code,first));self.assertEqual(len(calls),1)
   report=c.workspace/'run_report.json';v=json.loads(report.read_text());v['tests']=['changed report only'];report.write_text(json.dumps(v))
   self.assertEqual(c.submit(c.session_id),(code,first));self.assertEqual(len(calls),1)
   self.assertEqual(len(c.lifecycle.records),0)
   with self.assertRaisesRegex(RuntimeError,'automatic replay is forbidden'):
    BuilderSessionController(run_dir=c.run_dir,workspace=c.workspace,source=c.source,evaluate=evaluate,require_native_evidence=False)
 def test_socket_flush_full_feedback_and_exact_ack_then_idempotent_freeze(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);calls=[]
   def evaluate(n,p):
    calls.append(n);return {case:semantic_fixture(root/'scores'/str(n)/case,case,tree_digest(p),30+n) for case in DEV}
   c=self.setup_controller(root,evaluate);c.start()
   try:
    first=self.call(c,action='submit');self.assertEqual(first['status'],200)
    deadline=time.monotonic()+2
    while not c.deliveries and time.monotonic()<deadline:time.sleep(.01)
    self.assertTrue(c.deliveries);payload=first['payload'];self.assertEqual(payload['feedback'],json.loads(Path(c.submissions[0]['feedback']).read_text()))
    self.assertNotIn('delivery_snapshot',json.dumps(payload));self.assertNotIn('contract_path',json.dumps(payload))
    self.write_delivery(c,'two');self.assertEqual(self.call(c,action='submit')['status'],409)
    second=self.call(c,action='submit',feedback_digest=payload['feedback_digest']);self.assertEqual(second['status'],200)
    deadline=time.monotonic()+2
    while len(c.deliveries)<2 and time.monotonic()<deadline:time.sleep(.01)
    self.assertEqual(c.submissions[1]['feedback_digest_ack'],payload['feedback_digest']);self.assertEqual(calls,[1,2])
    c.freeze_on_builder_exit();repeat=self.call(c,action='submit',feedback_digest=payload['feedback_digest']);self.assertEqual(repeat,second);self.assertEqual(calls,[1,2])
    self.assertEqual([x['state'] for x in c.intents.values()],['completed','completed'])
   finally:c.close()
 def test_successful_evaluation_survives_missing_delivery(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);calls=[]
   def evaluate(n,p):calls.append(n);return {case:semantic_fixture(root/'scores'/str(n)/case,case,tree_digest(p),30) for case in DEV}
   c=self.setup_controller(root,evaluate);first=c.submit(c.session_id);self.assertEqual(first[0],200)
   # Native delivery failed after evaluation: no successful socket flush receipt.
   self.assertFalse(c.deliveries);self.assertIsNone(c.submissions[0]['feedback_delivered_at'])
   self.assertEqual(c.submit(c.session_id),first);self.assertEqual(calls,[1])
   self.write_delivery(c,'two');self.assertEqual(c.submit(c.session_id,first[1]['feedback_digest'])[0],409)
   self.assertEqual(next(iter(json.loads(c.intent_path.read_text()).values()))['state'],'completed')
 def test_native_required_by_default(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);source=root/'source';source.mkdir();workspace=root/'workspace';workspace.mkdir();calls=[]
   c=BuilderSessionController(run_dir=root/'run',workspace=workspace,source=source,evaluate=lambda *x:calls.append(x))
   self.assertIsNone(c.session_id);self.assertEqual(c.submit('invented-session')[0],503);self.assertEqual(calls,[])
 def test_reconnection_requires_real_later_terminal(self):
  base=[{'type':'turn.started'},{'type':'error','message':'Reconnecting... 1/5 (stream ended)'},{'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]
  self.assertTrue(classify_native_termination(base)['successful_terminal'])
  for events in [base[:-1],base+[{'type':'turn.failed'}],base+[{'type':'error','message':'fatal synthetic failure'}]]:
   self.assertFalse(classify_native_termination(events)['successful_terminal'])
if __name__=='__main__':unittest.main()
