"""Offline actual import/Controller route plus conservative attribution controls."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import subprocess
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'harbor'));sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
from agentloop.import_attribution import decide, read, sha, verify_failure
from agentloop.public_feedback import public_case_feedback
from harbor import formal_one_stop as formal
PRIOR=Path('@@AGENTSWE_EDITING_RUNS@@/smoke/deeptutor/0910-native-direct-public-001-resume-002')
CANDIDATE=PRIOR/'builds/candidate_001_attempt_002/repository'
PYTHON='@@AGENTSWE_ENVS@@/deeptutor-task-env-v1/venv/bin/python'


class ImportAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output=Path(tempfile.mkdtemp(prefix='verification-',dir=ROOT.parent))
        before=read(PRIOR/'lifecycle/infrastructure_attempt_001.json')['dev_results']['dev_001']['broker_before']
        cls.history_sha=sha(PRIOR/'lifecycle/infrastructure_attempt_001.json')
        with patch.object(formal,'read_broker_stats',return_value=before):
            cls.result=formal.run_public_case(case_id='dev_001',repository=CANDIDATE,output=cls.output/'circular',broker_endpoint='http://127.0.0.1:1/v1/responses',runtime_python=PYTHON,result_judge_endpoint='http://127.0.0.1:1/v1/responses')
            cls.missing=formal.run_public_case(case_id='dev_002',repository=CANDIDATE,output=cls.output/'missing-dependencies',broker_endpoint='http://127.0.0.1:1/v1/responses',runtime_python='/usr/bin/python3',result_judge_endpoint='http://127.0.0.1:1/v1/responses')
        cls.launcher=read(cls.output/'circular/launcher_result.json')
        cls.attr=read(cls.launcher['import_attribution']['evidence_path'])
        cls.c=read(cls.attr['candidate_probe']);cls.b=read(cls.attr['baseline_probe']);cls.resources=cls.attr['resource_contract']

    def test_actual_original_candidate_attribution(self):
        self.assertEqual(self.result['classification'],'candidate_zero',self.result.get('reason'))
        self.assertEqual(self.result['score'],0)
        self.assertFalse(self.result['lower_agent_executed'])
        self.assertFalse(self.result['real_execution'])
        self.assertTrue(self.result['execution_attempted'])
        self.assertEqual(self.result['execution_phase'],'product_import')
        self.assertEqual(self.result['broker_delta']['calls'],0)
        contract=read(self.output/'circular/semantic_scoring/execution_result.json') if (self.output/'circular/semantic_scoring/execution_result.json').exists() else self.result['semantic_judgement']
        self.assertNotEqual(contract.get('judge_invoked'),True)

    def test_paired_actual_resource_scope(self):
        group=self.resources['observed']['cgroup']
        self.assertEqual(self.c['preflight_resource_contract']['cgroup'],group)
        self.assertEqual(self.b['preflight_resource_contract']['cgroup'],group)
        self.assertTrue(self.resources['valid']);self.assertTrue(self.resources['inherited_case_scope'])
        outer=self.launcher['case_resource_contract']
        self.assertEqual(group,outer['observed']['cgroup'])
        self.assertTrue(outer['cleanup']['complete'])
        self.assertIn('0::'+group,read(self.output/'circular/source-copy-observation.json')['cgroup'])
        self.assertEqual(self.launcher['runtime_probe']['preflight_resource_contract']['observed']['cgroup'],group)
        self.assertEqual(self.c['sandbox']['network_namespace'],'isolated')
        self.assertEqual(self.b['sandbox']['network_namespace'],'isolated')

    def test_actual_missing_dependencies_stays_infrastructure(self):
        self.assertEqual(self.missing['classification'],'infrastructure_invalid')
        self.assertIsNone(self.missing['score'])
        self.assertEqual(self.missing['broker_delta']['calls'],0)
        self.assertNotIn('public_diagnostic',self.missing)

    def test_baseline_import_failure_never_candidate_zero(self):
        b=copy.deepcopy(self.b);b['observed']['mastery_tool_error']='same circular import'
        self.assertFalse(decide(self.c,b,self.resources)['valid'])

    def test_baseline_dependency_failure_never_candidate_zero(self):
        b=copy.deepcopy(self.b);b['infra_valid']=False;b['observed']['imports']['pydantic']['ok']=False
        self.assertFalse(decide(self.c,b,self.resources)['valid'])

    def test_dependency_path_change_stays_unknown(self):
        c=copy.deepcopy(self.c);c['observed']['imports']['openai']['file']='/candidate/openai.py'
        self.assertFalse(decide(c,self.b,self.resources)['valid'])

    def test_scope_mismatch_stays_unknown(self):
        c=copy.deepcopy(self.c);c['preflight_resource_contract']['cgroup']='unverified'
        self.assertFalse(decide(c,self.b,self.resources)['valid'])

    def test_resource_failure_stays_unknown(self):
        r=copy.deepcopy(self.resources);r['timed_out']=True
        self.assertFalse(decide(self.c,self.b,r)['valid'])

    def test_generic_importerror_is_not_circular_attribution(self):
        c=copy.deepcopy(self.c);c['observed']['mastery_tool_exception']['type']='ModuleNotFoundError'
        self.assertFalse(decide(c,self.b,self.resources)['valid'])

    def test_unmodified_source_stays_unknown(self):
        c=copy.deepcopy(self.c);c['observed']['mastery_tool_exception']['frames']=[{'filename':str(CANDIDATE/'deeptutor/capabilities/__init__.py')}]
        self.assertFalse(decide(c,self.b,self.resources)['valid'])

    def test_diagnostic_public_allowlist(self):
        value=copy.deepcopy(self.result);value['public_diagnostic']['raw_traceback']='private secret';value['runtime_probe']='private fixture'
        feedback=public_case_feedback(value);text=json.dumps(feedback)
        self.assertIn('MASTERY_TOOL_TYPES',text)
        self.assertIn('adaptive_tools.py',text)
        for private in ('raw_traceback','private secret','/data/','/home/','baseline_probe','resource_contract','runtime_probe'):
            self.assertNotIn(private,text)

    def test_tampered_attribution_is_rejected(self):
        launch=copy.deepcopy(self.launcher);launch['import_attribution']['evidence_sha256']='0'*64
        self.assertIsNone(verify_failure(launch,self.output/'circular',self.result['candidate_digest']))

    def test_foreign_baseline_evidence_is_rejected(self):
        launch=copy.deepcopy(self.launcher);attr=copy.deepcopy(self.attr)
        folder=self.output/'circular/foreign-baseline';folder.mkdir()
        baseline=copy.deepcopy(self.b);baseline['repository']=str(CANDIDATE)
        path=folder/'baseline.json';path.write_text(json.dumps(baseline))
        attr['baseline_probe']=str(path);attr['baseline_probe_sha256']=sha(path)
        evidence=folder/'attribution.json';evidence.write_text(json.dumps(attr))
        launch['import_attribution']['evidence_path']=str(evidence);launch['import_attribution']['evidence_sha256']=sha(evidence)
        self.assertIsNone(verify_failure(launch,self.output/'circular',self.result['candidate_digest']))

    def test_expired_deadline_never_starts_a_child(self):
        from agentloop.import_attribution import bounded_run
        with patch('subprocess.Popen') as popen:
            with self.assertRaises(RuntimeError):
                bounded_run(['false'],cwd='/',env={},deadline=time.monotonic()-1)
            popen.assert_not_called()

    def test_actual_inherited_timeout_reaps_worker(self):
        from agentloop.owned_resources import run_owned
        output=self.output/'timeout-control';output.mkdir()
        child_code = "import os,time,json;from pathlib import Path;p=Path("+repr(str(output/'child.json'))+");p.write_text(json.dumps({'pid':os.getpid(),'cgroup':Path('/proc/self/cgroup').read_text()}));time.sleep(30)"
        code = "import sys,time,json,subprocess;from pathlib import Path;sys.path.insert(0,"+repr(str(ROOT/'agentloop'))+");from import_attribution import inherited_runner;out=Path("+repr(str(output))+");runner=inherited_runner(out,time.monotonic()+.5)\ntry:\n runner(["+repr(PYTHON)+",'-c',"+repr(child_code)+"],cwd='/',env={'PATH':'/usr/bin:/bin'},output=out/'unused',timeout=30)\nexcept subprocess.TimeoutExpired:\n (out/'timed_out').write_text('true')\n"
        completed,resources=run_owned([PYTHON,'-I','-c',code],cwd='/',env={'PATH':'/usr/bin:/bin'},output=output/'case_resources',timeout=3)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertTrue((output/'timed_out').exists())
        child=read(output/'child.json')
        self.assertIn('0::'+resources['observed']['cgroup'],child['cgroup'])
        self.assertFalse(Path('/proc/'+str(child['pid'])).exists())
        self.assertTrue(resources['cleanup']['complete'])
        (output/'verification.json').write_text(json.dumps({'valid':True,'resources':resources,'child':child}))

    def test_original_evidence_is_unchanged(self):
        self.assertEqual(sha(PRIOR/'lifecycle/infrastructure_attempt_001.json'),self.history_sha)


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ImportAttribution)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if hasattr(ImportAttribution,'output'):
        value={'valid':result.wasSuccessful(),'tests':result.testsRun,'external_model_api_calls':0,
            'model_endpoint':'unreachable localhost only; broker counters replayed as read-only fixtures',
            'candidate_source':str(CANDIDATE),'candidate_digest':ImportAttribution.result['candidate_digest'],
            'actual_candidate_imports':True,'actual_baseline_imports':True,'actual_public_case_route':True,
            'actual_import_resource_scope':ImportAttribution.resources,'public_diagnostic':ImportAttribution.result.get('public_diagnostic'),
            'production_and_historical_run_unmodified':True,'output':str(ImportAttribution.output)}
        (ImportAttribution.output/'verification.json').write_text(json.dumps(value,indent=2)+'\n')
        print('VERIFICATION',ImportAttribution.output/'verification.json')
    raise SystemExit(not result.wasSuccessful())
