import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import audit_readiness
import readiness_admission as admission

class ControlTests(unittest.TestCase):
    def test_old_self_asserted_admission_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve()
            (root/'t.json').write_text(json.dumps({'schema_version':'agentswe-edit-readiness-admission/v1', 'readiness':'READY','task':'t'}))
            ok,ref=admission.check_admission(root,task='t',sibling_digest='a'*64,contract_path=root/'c',smoke_path=root/'s')
            self.assertFalse(ok);self.assertIn('schema',ref['error'])
    def test_contract_ready_without_external_evidence_cannot_promote_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();tasks={};snap={'tasks':{}}
            for i in range(10):
                t='t'+str(i);p=root/t;(p/'meta').mkdir(parents=True)
                (p/'meta/0905_case_contract.json').write_text(json.dumps({'task':t,'sibling':str(p),'readiness':'READY'}))
                tasks[t]=p;snap['tasks'][t]={'sibling':{'digest':audit_readiness.tree_digest(p)}}
            (root/'post_repair_tree_snapshot.json').write_text(json.dumps(snap))
            cfg=SimpleNamespace(TASKS=tasks,SMOKE_ROOT=root/'smoke')
            with patch.object(audit_readiness,'ROOT',root),patch.object(audit_readiness,'load_config',return_value=cfg),patch('sys.argv',['audit','--matrix',str(root/'matrix.json'),'--gate',str(root/'gate.json')]),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(audit_readiness.main(),2)
            gate=json.loads((root/'gate.json').read_text())
            self.assertEqual(gate['profile'],admission.PROFILE)
            self.assertEqual(gate['ready_count'],0)
            self.assertFalse(gate['formal_launch_authorized'])
            self.assertTrue((root/'configuration_delta_registry.lock').is_file())
    def test_real_formal_entry_rejects_forged_10_of_10_old_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();sibling=root/'t';(sibling/'meta').mkdir(parents=True)
            (sibling/'meta/0905_case_contract.json').write_text(json.dumps({'task':'t','sibling':str(sibling)}))
            tasks={'t':str(sibling),**{f't{i}':str(root/f't{i}') for i in range(9)}}
            (root/'formal_config.py').write_text('from pathlib import Path\nTASKS='+repr(tasks)+'\nSMOKE_ROOT=Path('+repr(str(root/'smoke'))+')\n')
            gate={'ready_count':10,'expected_count':10, 'formal_ready':True,'source_unchanged':True,'formal_launch_authorized':True,'formal_launch_authorized_by_gate':True,'tasks':[{'task':t,'readiness':'READY','errors':[]} for t in tasks]}
            (root/'formal_readiness_gate.json').write_text(json.dumps(gate))
            with patch.object(admission,'ROOT',root),self.assertRaisesRegex(ValueError,'current READY readiness gate'):
                admission.require_formal_readiness(sibling)
if __name__=='__main__':unittest.main()
