"""Actual historical Candidate SDK/real tool consumption; synthetic upstream only."""
import json,sys,threading,subprocess,shutil,socket,time,asyncio,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[2];H=ROOT/'evaluator/harness';sys.path.insert(0,str(H))
from owned_resources import run_owned
from transport_sandbox import FixedLowerRelay
from deepcode_lower_agent import sandbox_command,candidate_environment
WORKER=r'''import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,'/candidate')
from core.providers.openai_compat import OpenAICompatProvider
from tools.code_implementation_server import initialize_workspace,read_file
async def main():
 mode=sys.argv[1];p=OpenAICompatProvider(api_key='broker-only-placeholder',api_base=os.environ['OPENAI_BASE_URL'],default_model='gpt-5.6-sol')
 messages=[{'role':'user','content':'native-probe:'+mode}]
 tools=[{'type':'function','function':{'name':'read_file','description':'Read actual local bytes','parameters':{'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path']}}}]
 def evidence(r):return {'content':r.content,'finish_reason':r.finish_reason,'usage':r.usage,'tools':[{'id':t.id,'name':t.name,'arguments':t.arguments} for t in r.tool_calls]}
 first=await p.chat_stream(messages,tools=tools,reasoning_effort='high');result={'mode':mode,'first':evidence(first)}
 if mode=='tool':
  assert len(first.tool_calls)==1
  tool=first.tool_calls[0];assert tool.name=='read_file'
  initialize_workspace('/workspace');actual=await read_file(**tool.arguments);result['actual_native_tool_result']=json.loads(actual)
  messages += [{'role':'assistant','content':first.content,'tool_calls':[{'id':tool.id,'type':'function','function':{'name':tool.name,'arguments':json.dumps(tool.arguments)}}]},{'role':'tool','tool_call_id':tool.id,'content':actual}]
  result['final']=evidence(await p.chat_stream(messages,tools=tools,reasoning_effort='high'))
 if mode in ('partial','unknown'):
  result['retry']=evidence(await p.chat_stream(messages,tools=tools,reasoning_effort='high'))
 await p._client.close();Path('/runtime/sdk-result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
asyncio.run(main())
'''
def main():
 import argparse
 ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=a.output;out.mkdir(parents=True,exist_ok=False)
 source=Path('@@AGENTSWE_EDITING_RUNS@@/smoke/deepcode/0905-smoke-current-004/lifecycle/frozen_candidate');repo=out/'candidate';shutil.copytree(source,repo,symlinks=True)
 for p in [repo,*repo.rglob('*')]:
  if not p.is_symlink():p.chmod(p.stat().st_mode|0o200)
 python=Path('@@AGENTSWE_ENVS@@/deepcode-runtime-venv/bin/python')
 requests=[]
 class Upstream(BaseHTTPRequestHandler):
  def log_message(self,*a):pass
  def do_POST(self):
   value=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(value);(out/'upstream-requests.json').write_text(json.dumps(requests,indent=2))
   mode=next(m['content'].split(':')[-1] for m in value['input'] if m.get('role')=='user');status=200;ct='text/event-stream'
   if mode=='unknown':status=524;b=b'upstream uncertain'
   elif mode=='partial':b=b'data: {"type":"response.output_text.delta","delta":"uncommitted partial"}\n\n'
   else:
    tool_results=[m for m in value['input'] if m.get('type')=='function_call_output']
    if mode=='tool' and not tool_results:output=[{'type':'function_call','id':'fc_native','call_id':'call_native','name':'read_file','arguments':'{"file_path":"transport-probe.txt"}'}]
    elif mode=='empty':output=[]
    else:
     text='native-tool-returned:'+json.loads(tool_results[-1]['output'])['content'] if tool_results else 'native streaming text'
     output=[{'id':'msg_native','type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]
    response={'id':'resp_native_'+str(len(requests)),'status':'completed','output':output,'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
    b=('data: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode()
   self.send_response(status);self.send_header('Content-Type',ct);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 server=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=server.serve_forever,daemon=True).start()
 key=out/'synthetic.env';key.write_text('GATEWAY_API_KEY=synthetic-never-real\n');sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
 log=(out/'broker.log').open('w');broker=subprocess.Popen([sys.executable,'-I',str(H/'broker_server.py'),'--port',str(port),'--upstream',f'http://127.0.0.1:{server.server_port}','--credential-file',str(key),'--stats-file',str(out/'stats.json')],stdout=log,stderr=log)
 checks={};results={}
 try:
  import urllib.request
  for _ in range(100):
   try:urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=.2).close();break
   except Exception:time.sleep(.02)
  for mode in ('text','tool','empty','partial','unknown'):
   case=out/mode;case.mkdir();work=case/'workspace';work.mkdir();(work/'transport-probe.txt').write_text('actual native bytes')
   home=case/'home';home.mkdir();runtime=case/'runtime';runtime.mkdir();worker=runtime/'sdk-worker.py';worker.write_text(WORKER)
   before=len(requests);relay=FixedLowerRelay(f'http://127.0.0.1:{port}/v1/responses',context_identity='sdk-'+mode).start()
   try:
    cmd,_=sandbox_command(command=[str(python),'/runtime/sdk-worker.py',mode],repository=repo,workspace=work,home=home,isolated=runtime,python_executable=python,relay=relay)
    env,_=candidate_environment({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONPATH':'/candidate','PYTHONDONTWRITEBYTECODE':'1'})
    proc,att=run_owned(cmd,cwd=Path('/'),env=env,output=case/'resources',timeout=600)
    (case/'stdout.log').write_text(proc.stdout or '');(case/'stderr.log').write_text(proc.stderr or '')
   finally:relay.close()
   result=json.loads((runtime/'sdk-result.json').read_text()) if (runtime/'sdk-result.json').is_file() else {};result['upstream_calls']=len(requests)-before;result['exit_code']=proc.returncode;results[mode]=result
  checks['actual_sdk_streaming_text']=results['text'].get('first',{}).get('content')=='native streaming text'
  checks['actual_sdk_streaming_usage']=results['text'].get('first',{}).get('usage',{}).get('prompt_tokens')==3
  tool=results['tool'];checks['actual_sdk_tool_call_and_native_file_read']=tool.get('actual_native_tool_result',{}).get('content')=='actual native bytes'
  checks['actual_tool_return_is_in_next_upstream_turn']=tool.get('final',{}).get('content')=='native-tool-returned:actual native bytes' and tool['upstream_calls']==2
  checks['empty_completed_is_empty_not_fabricated']=results['empty'].get('first',{}).get('content') in (None,'') and not results['empty'].get('first',{}).get('tools') and results['empty']['upstream_calls']==1
  for mode in ('partial','unknown'):
   checks[mode+'_is_error_without_resampling']=results[mode].get('first',{}).get('finish_reason')=='error' and results[mode].get('retry',{}).get('finish_reason')=='error' and results[mode]['upstream_calls']==1
  report={'passed':all(checks.values()),'checks':checks,'results':results,'real_provider_calls':0,'actual_native_product_sdk':True,'mocked_sdk':False};(out/'verification.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'checks':checks}));return 0 if report['passed'] else 2
 finally:broker.terminate();broker.wait(timeout=5);log.close();server.shutdown();server.server_close()
if __name__=='__main__':raise SystemExit(main())
