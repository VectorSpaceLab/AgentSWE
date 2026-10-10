import copy
import json
import tempfile
import unittest
from pathlib import Path
from agentloop.evaluator.authority_world import specialize
from agentloop.evaluator.dynamic_case_service import issue
from agentloop.evaluator.case_contract import validate_private_oracle, validate_visible_case, require_decision_authority
from agentloop.evaluator.lower_agent_launcher import hook_observation


def template():
    def hook(i,tool='Write'):
        return {'id':i,'type':'hook_event','event':{'hook_event_name':'PreToolUse','tool_name':tool,'session_id':'fixture-session','tool_use_id':i,'tool_input':{'file_path':'src/work.txt'}}}
    acts=[hook('attempt_tmp_write'),hook('legacy_source_write'),hook('read_source','Read'),hook('safe_shell','Bash'),hook('blocked_network','Bash'),hook('observe_authorized_pre'),hook('probe_missing_delegation','mcp__files__inspect'),hook('probe_source_after_prepare','Read'),hook('observe_crashed_pre')]
    acts.append({'id':'prepare_recovery_handoff','type':'inspector_command','argv':['--prepare-handoff'],'stdin':{'target_instance_id':'recovery-a'}})
    p={'schema_version':2,'tools':{'Read':'allow','Write':'allow'},'commands':{'rewrites':[]},'network':{'allow_hosts':[],'deny_hosts':[]},'delivery':{'max_pending':8},'mcp':{'mcp__files__inspect':{'require_delegation':True}},'ownership':{'allowed_targets':['recovery-a']},'reservation':{'lease_seconds':3}}
    return {'candidate':{'task':'Inspect actual authority.','policy':p,'allowed_actions':acts,'initial_events':[copy.deepcopy(acts[-2]['event'])]},'private_oracle':{'required_action_ids':[],'expected_permissions':{a['id']:'allow' for a in acts if a['type']=='hook_event'}}}


class AuthorityWorldTest(unittest.TestCase):
    def test_each_world_changes_authority_and_binds_oracle(self):
        for cid in ['dev_001','dev_002']+['test_%03d'%i for i in range(1,7)]:
            with self.subTest(case=cid), tempfile.TemporaryDirectory() as td:
                t=template(); pairs=[]
                for branch in (0,1):
                    d=Path(td)/str(branch)
                    x=issue(cid,t,d,d,branch=branch)
                    v=json.loads(Path(x['candidate_payload']).read_text());o=json.loads(Path(x['oracle_path']).read_text());pairs.append((v,o))
                    validate_private_oracle(o,v,case_id=cid)
                    require_decision_authority(v,o)
                    with self.assertRaises(ValueError):require_decision_authority(v,{k:z for k,z in o.items() if k != "authority_world"})
                    self.assertNotIn('authority_world',v)
                    self.assertNotIn('expected_permissions',json.dumps(v))
                    self.assertNotIn('oracle',json.dumps(v))
                    with self.assertRaises(FileExistsError):issue(cid,t,d,d,branch=branch)
                    v['policy']['tools']['Write']='forged'
                    with self.assertRaisesRegex(ValueError,'actual authority world'):validate_private_oracle(o,v,case_id=cid)
                self.assertNotEqual(pairs[0][1]['expected_permissions'],pairs[1][1]['expected_permissions'])
                self.assertNotEqual(pairs[0][1]['authority_world']['current_policy'],pairs[1][1]['authority_world']['current_policy'])

    def test_nested_protocol_is_the_only_permission_source(self):
        self.assertEqual(hook_observation({'permission':'allow'}),(None,None))
        self.assertEqual(hook_observation({'permission':'allow','hookSpecificOutput':{'permissionDecision':'deny'},'policyReceipt':{'decision':'allow'}}),('deny','allow'))

    def test_fixture_age_is_bounded_and_requires_actual_prefix(self):
        v=template()['candidate'];v['case_id']='dev_001';v['fixture_age_seconds']=6
        with self.assertRaises(ValueError):validate_visible_case(v,hidden=False)
        v['fixture_age_seconds']=2;v.pop('initial_events')
        with self.assertRaises(ValueError):validate_visible_case(v,hidden=False)
