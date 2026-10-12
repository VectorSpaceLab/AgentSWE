"""Build-boundary tests, not semantic handoff cases or reference solutions."""
from __future__ import annotations
import json
import copy
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from evaluator import candidate_runtime as build
from lower_agent import owned_resources as owned
from lower_agent.build_sandbox import build_command,run_build_command
from lower_agent.launcher import materialize_product
from harbor.formal_one_stop import unavailable_runtime_results,allocate_runtime_attempt

RUNTIME=Path('@@AGENTSWE_ENVS@@/openclaw-channel-handoff-ledger-edit-v1')


class CandidateBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='oc-build-test-')
        self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_case_budget_cannot_use_the_new_explicit_build_limit(self):
        with patch.object(owned.subprocess,'Popen') as popen:
            for kwargs in ({'timeout':931},{'timeout':1801,'purpose':'build'},
                           {'timeout':1,'purpose':'anything'},{'timeout':float('nan'),'purpose':'build'}):
                with self.assertRaises(ValueError):
                    owned.run_owned(['/bin/true'],cwd=self.root,env={},output=self.root/'invalid',**kwargs)
            popen.assert_not_called()
        with patch.object(owned.subprocess,'Popen',side_effect=OSError('synthetic spawn refusal')) as popen:
            with self.assertRaises(OSError):
                owned.run_owned(['/bin/true'],cwd=self.root,env={},output=self.root/'build',timeout=900,purpose='build')
            self.assertIn('--property=RuntimeMaxSec=900s',popen.call_args.args[0])

    def test_build_deadline_and_existing_output_fail_before_dispatch(self):
        candidate=self.root/'candidate';candidate.mkdir()
        with patch.object(build,'run_owned') as run:
            for seconds in (0,1801,float('inf'),float('nan')):
                with self.assertRaises(ValueError):
                    build.prepare_candidate_runtime(candidate=candidate,output=self.root/'out',runtime=None,timeout_seconds=seconds)
            with self.assertRaises(ValueError):
                build.prepare_candidate_runtime(candidate=candidate,output=self.root/'out',runtime=None,deadline_monotonic=time.monotonic()-1)
            output=self.root/'existing';output.mkdir()
            with self.assertRaises(FileExistsError):
                build.prepare_candidate_runtime(candidate=candidate,output=output,runtime=None)
            run.assert_not_called()

    def test_worker_environment_and_failure_attribution(self):
        candidate=self.root/'candidate';candidate.mkdir()
        resource={'valid':True,'timed_out':False,'cleanup':{'complete':True}}
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'SYNTHETIC-NOT-A-REAL-KEY','NODE_OPTIONS':'--synthetic-invalid'}),\
             patch.object(build,'run_owned',return_value=(subprocess.CompletedProcess([],1,'',''),resource)) as run:
            value=build.prepare_candidate_runtime(candidate=candidate,output=self.root/'out',runtime=None)
        self.assertEqual(value['classification'],'build_infrastructure_error')
        self.assertFalse(value['candidate_runtime_ready'])
        self.assertEqual(set(run.call_args.kwargs['env']),{'PATH','LANG'})
        self.assertEqual(run.call_args.kwargs['purpose'],'build')
        self.assertLessEqual(run.call_args.kwargs['timeout'],1800)

    def test_build_outputs_cannot_mutate_frozen_source(self):
        candidate=self.root/'candidate';candidate.mkdir()
        with patch.object(build,'run_owned') as run:
            with self.assertRaises(ValueError):
                build.prepare_candidate_runtime(candidate=candidate,output=candidate/'product',runtime=None)
            run.assert_not_called()
        self.assertEqual(list(candidate.iterdir()),[])

    def test_retry_allocates_new_files_without_replacing_prior_evidence(self):
        first,manifest=allocate_runtime_attempt(self.root,1)
        manifest.write_text('retained infrastructure failure')
        second,next_manifest=allocate_runtime_attempt(self.root,1)
        self.assertNotEqual(first,second)
        self.assertNotEqual(manifest,next_manifest)
        self.assertEqual(manifest.read_text(),'retained infrastructure failure')

    def test_infrastructure_or_unknown_build_does_not_become_candidate_zero(self):
        for reason in ('build_infrastructure_error','build_source_integrity_error','unknown',None):
            result=unavailable_runtime_results({'classification':reason},self.root/'manifest',('dev_001','dev_002'))
            self.assertTrue(all(value['classification']=='infrastructure-invalid' for value in result.values()))
        result=unavailable_runtime_results({'classification':'candidate_build_failure'},self.root/'manifest',('dev_001',))
        self.assertEqual(result['dev_001']['classification'],'candidate_build_failure')

    def prepared_product(self,name):
        root=self.root/name
        (root/'src/gateway').mkdir(parents=True)
        (root/'src/gateway/native.ts').write_text('candidate source')
        (root/'dist').mkdir();(root/'dist/entry.js').write_text('candidate compiled')
        (root/'node_modules/pkg').mkdir(parents=True)
        (root/'node_modules/pkg/index.js').write_text('candidate dependency')
        return root

    def test_materialization_copies_candidate_dependencies_and_never_seeds_baseline(self):
        source=self.prepared_product('source')
        baseline=self.prepared_product('baseline')
        (baseline/'dist/entry.js').write_text('WRONG BASELINE')
        (baseline/'node_modules/pkg/index.js').write_text('WRONG DEPENDENCY')
        output=self.root/'case';output.mkdir()
        product=materialize_product(source,output,{'prebuilt_dist':baseline/'dist','node_modules':baseline/'node_modules'})
        self.assertEqual(build.tree_digest(source),build.tree_digest(product))
        self.assertEqual((product/'node_modules/pkg/index.js').read_text(),'candidate dependency')
        with self.assertRaises(FileExistsError):materialize_product(source,output,{})
        (source/'dist/entry.js').unlink()
        empty=self.root/'next';empty.mkdir()
        with self.assertRaisesRegex(RuntimeError,'fallback is forbidden'):
            materialize_product(source,empty,{'prebuilt_dist':baseline/'dist','node_modules':baseline/'node_modules'})

    def test_runtime_gate_checks_actual_compiled_dependencies_and_source_bytes(self):
        frozen=self.root/'frozen';(frozen/'src/gateway').mkdir(parents=True)
        (frozen/'src/gateway/native.ts').write_text('candidate source')
        product=self.prepared_product('image')
        expected=build.tree_digest(frozen)
        manifest={'schema_version':'openclaw-candidate-runtime-v2','candidate_runtime_ready':True,
            'candidate_source_digest':expected,'candidate_source_digest_after_build':expected,
            'source_digest_stable':True,'protected_source_drift':[],'runtime_product':str(product),
            'build_resources':{'valid':True,'cleanup':{'complete':True},'timed_out':False},
            'build_phases':[{'phase':name,'valid':True,'timed_out':False,'exit_code':0} for name in
                ('git-init','offline-install','strict-build','focused-tests','entry-probe')],**build.runtime_binding(product)}
        self.assertEqual(build.validate_runtime_manifest(manifest,frozen),[])
        for relative in ('dist/entry.js','node_modules/pkg/index.js','src/gateway/native.ts'):
            path=product/relative;original=path.read_text();path.write_text('TAMPERED')
            self.assertTrue(build.validate_runtime_manifest(manifest,frozen),relative)
            path.write_text(original)
        broken=copy.deepcopy(manifest);broken['build_phases'][1]['exit_code']=1
        self.assertIn('native build phase failed or unverified',build.validate_runtime_manifest(broken,frozen))

    def test_runtime_compiled_links_and_dist_runtime_are_bound(self):
        root=self.prepared_product('compiled')
        (root/'dist-runtime').mkdir();runtime=root/'dist-runtime/native.js';runtime.write_text('first')
        before=build.compiled_digest(root);runtime.write_text('second')
        self.assertNotEqual(before,build.compiled_digest(root))
        link=root/'dist/link';link.symlink_to('entry.js')
        before=build.compiled_digest(root);link.unlink();link.symlink_to('../dist-runtime/native.js')
        self.assertNotEqual(before,build.compiled_digest(root))

    def test_core_source_bytes_are_bound_and_external_links_rejected(self):
        root=self.root/'candidate';(root/'src/gateway').mkdir(parents=True)
        source=root/'src/gateway/feature.ts';source.write_text('before')
        before=build.protected_source_inventory(root)
        source.write_text('after')
        self.assertNotEqual(before,build.protected_source_inventory(root))
        private=self.root/'private';private.write_text('synthetic')
        (root/'src/gateway/link.ts').symlink_to(private)
        with self.assertRaisesRegex(ValueError,'escapes'):
            build.protected_source_inventory(root)

    def test_actual_build_namespace_has_no_host_private_path_credentials_or_network(self):
        if not Path('/usr/bin/bwrap').is_file() or not RUNTIME.is_dir():
            self.skipTest('requires actual Linux pinned build environment')
        product=self.root/'scope/product';product.mkdir(parents=True)
        private=self.root/'private-evaluator.txt';private.write_text('synthetic-private')
        pinned={'root':str(RUNTIME),'node':str(RUNTIME/'bin/node')}
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            script=('import json,os,socket;from pathlib import Path;'
                's=socket.socket();s.settimeout(.2);'
                f'print(json.dumps({{"private_visible":Path({str(private)!r}).exists(),'
                '"api":os.environ.get("DEEPSEEK_API_KEY"),"node_options":os.environ.get("NODE_OPTIONS"),'
                '"proxy":os.environ.get("HTTP_PROXY"),'
                f'"host_connect_result":s.connect_ex(("127.0.0.1",{listener.getsockname()[1]}))}}))')
            with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'SYNTHETIC','NODE_OPTIONS':'--bad','HTTP_PROXY':'http://invalid'}):
                value=run_build_command(['/usr/bin/python3','-I','-c',script],product=product,pinned=pinned,
                                        evidence=self.root/'actual',deadline=time.monotonic()+10)
        self.assertTrue(value['valid'],value)
        self.assertEqual(value['exit_code'],0)
        observation=json.loads((self.root/'actual/stdout.log').read_text())
        self.assertEqual(observation,{'private_visible':False,'api':None,'node_options':None,'proxy':None,
                                      'host_connect_result':111})
        with self.assertRaises(ValueError):
            build_command(['/bin/true'],product=product,pinned=pinned,extra_environment={'OPENAI_API_KEY':'synthetic'})


if __name__=='__main__':unittest.main()
