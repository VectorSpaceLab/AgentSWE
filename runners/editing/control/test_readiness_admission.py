"""Disk-backed v2 admission/coordinator integration; fixture judges are synthetic."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import readiness_admission as admission
import readiness_coordinator as coordinator
import readiness_judge_validation as schema
import audit_readiness as audit
import v2_usage_normalizers as usage
from v2_test_fixtures import Bundle, VALIDATORS, NORMALIZERS

class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.source=self.root/'source';(self.source/'meta').mkdir(parents=True)
        self.contract=self.source/'meta/0905_case_contract.json'
        self.contract.write_text(json.dumps({'task':'t','sibling':str(self.source),'readiness':'REPAIR'}))
        (self.root/'configuration_delta_registry.json').write_text(json.dumps(
            {'schema_version':'agentswe-edit-configuration-registry/v1',
             'tasks':{'t':{'sibling':str(self.source),'status':'REPAIR','effective_source_files':[]}}}))
        self.binding={'task':'t','source_digest':audit.tree_digest(self.source),
            'contract_digest':admission.sha(self.contract),
            'registry_digest':admission.registry_digest(self.root/'configuration_delta_registry.json','t')}
        self.smoke_root=self.root/'smokes';self.bundle=self.smoke_root/'t'/'fresh-run'/'readiness_bundle';self.bundle.mkdir(parents=True)
        self.b=Bundle(self.bundle,binding=self.binding)
        self.manifest_sha=self.b.seal()
        (self.root/'post_repair_tree_snapshot.json').write_text(json.dumps({'tasks':{'t':{'sibling':{'digest':self.binding['source_digest']}}}}))
        self.review=self.root/'independent-review.json'
        self.review.write_text(json.dumps({'task':'t','profile':admission.PROFILE,'current_binding':self.binding,
            'manifest_sha256':self.manifest_sha,'review_checks':dict.fromkeys(admission.REQUIRED_CHECKS,True)}))
        self.cfg=SimpleNamespace(TASKS={'t':self.source},SMOKE_ROOT=self.smoke_root,AUTHORITATIVE_CREATE_CODE_JUDGE=self.root/'unused')
        for target,name,value in ((admission,'ROOT',self.root),(coordinator,'ROOT',self.root)):
            patcher=patch.object(target,name,value);patcher.start();self.addCleanup(patcher.stop)
        for target in (coordinator,schema):
            patcher=patch.object(target,'make_judge_output_validators',return_value=VALIDATORS);patcher.start();self.addCleanup(patcher.stop)
        for target in (coordinator,usage):
            patcher=patch.object(target,'make_broker_record_normalizers',return_value=NORMALIZERS);patcher.start();self.addCleanup(patcher.stop)
    def prepare(self):
        return coordinator.prepare_admission('t',self.bundle,self.review,admission.sha(self.review),self.cfg)
    def publish_fixture(self):
        latest,raw,value=self.prepare()
        latest.write_bytes(raw);ad=self.root/'readiness_admissions';ad.mkdir()
        (ad/'t.json').write_text(json.dumps(value))
        return latest,ad
    def verify(self,latest,ad):
        return admission.check_admission(ad,task='t',sibling_digest=audit.tree_digest(self.source),contract_path=self.contract,smoke_path=latest)
    def test_valid_bundle_prepare_has_no_side_effect_and_admission_preserves_contract(self):
        before=self.contract.read_bytes();latest,raw,value=self.prepare()
        self.assertFalse(latest.exists());self.assertFalse((self.root/'readiness_admissions').exists())
        latest,ad=self.publish_fixture();self.assertTrue(self.verify(latest,ad)[0])
        self.assertEqual(self.contract.read_bytes(),before)
    def test_evidence_byte_mutation_is_not_admitted(self):
        latest,ad=self.publish_fixture();(self.bundle/'s2/solution.patch').write_text('mutated')
        self.assertFalse(self.verify(latest,ad)[0])
    def test_registry_drift_is_not_admitted(self):
        latest,ad=self.publish_fixture();(self.root/'configuration_delta_registry.json').write_text('{"changed":true}')
        self.assertFalse(self.verify(latest,ad)[0])
    def test_current_source_drift_is_not_admitted(self):
        latest,ad=self.publish_fixture();(self.source/'new.py').write_text('changed')
        self.assertFalse(self.verify(latest,ad)[0])
    def test_review_hash_or_manifest_rebinding_cannot_authorize(self):
        self.review.write_text('{}')
        with self.assertRaises(ValueError):self.prepare()
        self.assertFalse((self.smoke_root/'t/latest_smoke_manifest.json').exists())
    def test_old_admission_version_is_rejected(self):
        latest,ad=self.publish_fixture();p=ad/'t.json';v=json.loads(p.read_text());v['schema_version']='agentswe-edit-readiness-admission/v1';p.write_text(json.dumps(v))
        self.assertFalse(self.verify(latest,ad)[0])
    def test_external_review_change_is_rejected(self):
        latest,ad=self.publish_fixture();self.review.write_text('{"changed":true}')
        self.assertFalse(self.verify(latest,ad)[0])

if __name__=='__main__':unittest.main()
