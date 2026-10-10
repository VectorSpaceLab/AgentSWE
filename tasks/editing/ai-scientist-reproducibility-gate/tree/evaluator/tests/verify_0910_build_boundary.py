"""Actual owned scope, bwrap, Git and native compiler; no external API."""
import argparse,hashlib,http.server,json,os,sys,threading
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from agentloop import candidate_adapter as adapter
from agentloop.protocol import tree_digest
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--real-source',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 private=a.output/'private-canary.txt';canary='OWNER_C_SYNTHETIC_PRIVATE_VALUE';private.write_text(canary)
 class Private(http.server.BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'private')
 server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Private);threading.Thread(target=server.serve_forever,daemon=True).start()
 original=tree_digest(a.real_source);rows=[];host_net=os.readlink('/proc/self/ns/net')
 def delivery(name,broken=False):
  path=a.output/'deliveries'/name;path.mkdir(parents=True)
  content='def broken(:' if broken else 'from pathlib import Path; Path("/output/CANDIDATE_CODE_EXECUTED").write_text("bad")'
  (path/'solution.patch').write_text('diff --git a/ai_scientist/_agentswe_compile_probe.py b/ai_scientist/_agentswe_compile_probe.py\nnew file mode 100644\n--- /dev/null\n+++ b/ai_scientist/_agentswe_compile_probe.py\n@@ -0,0 +1 @@\n+'+content+'\n')
  for name in ('edit_report.json','run_report.json'):(path/name).write_text('{}\n')
  return path
 def run(name,source,deliver,expected,overrides=None,scoped=True):
  out=a.output/'builds'/name;out.parent.mkdir(exist_ok=True)
  with patch.multiple(adapter,BOUNDARY_PROBE_PATHS=(str(private),),BOUNDARY_PROBE_PORT=server.server_port,**(overrides or {})):
   os.environ['AGENTSWE_BUILD_SYNTHETIC_SECRET']='synthetic-parent-value'
   try:result=adapter.build_candidate(source,deliver,out)
   finally:os.environ.pop('AGENTSWE_BUILD_SYNTHETIC_SECRET',None)
  manifest=json.loads((out/'build_manifest.json').read_text());resource=result.get('resource_contract') or {};health=result.get('infrastructure_health') or {}
  checks={'expected_classification':result['classification']==expected,'manifest_equals_return':manifest==result,
   'candidate_never_executed':result.get('candidate_code_executed') is False and not (out/'CANDIDATE_CODE_EXECUTED').exists(),
   'no_private_content_in_diagnostics':canary not in json.dumps(result) and canary not in (out/'build.stdout.log').read_text() and canary not in (out/'build.stderr.log').read_text(),
   'real_post_compile_digest':not (out/'repository').exists() or result.get('candidate_repo_digest')==tree_digest(out/'repository')}
  if scoped:
   checks.update(actual_owned_build_4GiB=resource.get('valid') is True and resource.get('purpose')=='build' and resource.get('memory_bytes')==4294967296,
     all_build_within_1800=resource.get('timeout_seconds',1801)<=1800 and result['elapsed_seconds']<=1800,
     complete_owned_cleanup=resource.get('cleanup',{}).get('complete') is True and resource.get('aggregate_cleanup',{}).get('complete',True) is True,
     resource_hash_bound=hashlib.sha256(Path(result['resource_attestation_path']).read_bytes()).hexdigest()==result['resource_attestation_sha256'])
  if 'private_probe_visible' in health:
   checks.update(worker_cannot_see_private=health['private_probe_visible'][str(private)] is False,
     host_network_inaccessible=health.get('host_loopback_reachable') is False and health['network_namespace']!=host_net,
     ambient_secret_not_inherited=health['synthetic_ambient_secret_absent'])
  if name=='syntax':
   checks.update(causal_only_after_healthy_baseline=result['causal_candidate_failure'] is True and health.get('valid') is True and health['baseline_compile']['valid'] is True,
     bounded_code_safe_diagnostics=0<len(result['compile_diagnostics'])<=20 and all('/data/' not in d['path'] and 'text' not in d and 'source' not in d for d in result['compile_diagnostics']))
  elif name!='normal':checks['infra_not_candidate_zero']=result['causal_candidate_failure'] is False
  else:checks['actual_baseline_and_patched_compile_healthy']=result['valid'] and health['baseline_compile']['python_files']>0 and result['python_compileall']
  row={'name':name,'passed':all(checks.values()),'checks':checks,'manifest':str(out/'build_manifest.json'),'manifest_sha256':hashlib.sha256((out/'build_manifest.json').read_bytes()).hexdigest(),'elapsed_seconds':result['elapsed_seconds']};rows.append(row);print(json.dumps(row),flush=True)
 try:
  run('normal',a.real_source,delivery('normal'),'candidate_ready')
  run('syntax',a.real_source,delivery('syntax',True),'candidate_build_failure')
  run('missing_git',a.real_source,delivery('missing_git'),'infrastructure_failure',{'GIT_EXECUTABLE':'/missing/git'})
  bad=a.output/'bad-source';bad.mkdir();(bad/'broken.py').write_text('def bad(:\n');run('bad_source',bad,delivery('bad_source'),'infrastructure_failure')
  linked=a.output/'linked-source';linked.mkdir();(linked/'x.py').symlink_to(private);run('external_source_link',linked,delivery('external_source_link'),'infrastructure_failure')
  patch_delivery=delivery('external_patch_link');(patch_delivery/'solution.patch').unlink();(patch_delivery/'solution.patch').symlink_to(private);run('external_patch_link',a.real_source,patch_delivery,'infrastructure_failure')
  rootlink=a.output/'source-root-link';rootlink.symlink_to(a.real_source,target_is_directory=True);run('source_root_link',rootlink,delivery('source_root_link'),'infrastructure_failure',scoped=False)
  run('missing_bwrap',a.real_source,delivery('missing_bwrap'),'infrastructure_failure',{'BWRAP_EXECUTABLE':'/missing/bwrap'})
  report={'passed':all(r['passed'] for r in rows) and tree_digest(a.real_source)==original,'cases':rows,'external_provider_calls':0,'real_source':str(a.real_source),'source_digest_before':original,'source_digest_after':tree_digest(a.real_source),'candidate_code_executed':False}
  (a.output/'verification.json').write_text(json.dumps(report,indent=2)+'\n');return 0 if report['passed'] else 2
 finally:server.shutdown();server.server_close()
if __name__=='__main__':raise SystemExit(main())

