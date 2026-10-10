"""Actual loopback HTTP controls; invoke in a network namespace with no egress."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
sys.path.insert(0,str(a.source));os.environ['AGENTSWE_EVALUATOR_PROXY_URL']=''
from agentloop import broker as b
key='synthetic-error-secret-only'
calls=[]
class Upstream(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']));calls.append(self.path)
  body=(b'<html>Cloudflare Error 1010: Access denied '+key.encode()+b'</html>') if self.path=='/403' else b'x'*(b.MAX_ERROR_CAPTURE_BYTES*2)
  self.send_response(403 if self.path=='/403' else 503)
  self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)))
  self.send_header('cf-ray','synthetic-ray');self.send_header('set-cookie','private-cookie='+key)
  self.send_header('x-request-id',key);self.end_headers()
  try:self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass

def server(handler):
 srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
 thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
 return srv,thread

def post(srv,context):
 req=urllib.request.Request(f'http://127.0.0.1:{srv.server_port}/v1/responses',data=json.dumps({'model':b.LOWER_MODEL,'reasoning':{'effort':b.LOWER_EFFORT},'input':'local error control','stream':False}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+b.PLACEHOLDER_TOKEN,'X-AgentSWE-Context':context})
 try:
  with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=10) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.load(e)

up,t=server(Upstream);proofs=[]
try:
 for code in (403,503):
  state=b.BrokerState(a.output/f'{code}/stats.json',0,0)
  srv,thread=server(b.make_handler(state,f'http://127.0.0.1:{up.server_port}/{code}',key))
  try:
   start=len(calls);status,payload=post(srv,str(code));assert status==502 and payload['retry_allowed'] is False
   stats=state.public();assert stats['calls']==1 and stats['successful_calls']==0 and stats['unknown_calls']==1 and stats['total_tokens'] is None
   row=stats['requests'][0];meta=row['http_error_evidence'];assert meta['http_status']==code
   request_id=row['request_sha256'];raw=a.output/f'{code}/stats-raw/{request_id}.bin';data=raw.read_bytes()
   assert len(data)<=b.MAX_ERROR_CAPTURE_BYTES and key.encode() not in data
   assert meta['raw_capture_sha256']==hashlib.sha256(data).hexdigest()
   assert 'set-cookie' not in meta['headers'] and key not in json.dumps(meta)
   if code==403:assert b'1010' in data and meta['body_complete']
   else:assert not meta['body_complete']
   assert '1010' not in json.dumps(payload) and 'http_error_evidence' not in payload
   status,_=post(srv,str(code));assert status==409 and len(calls)==start+1
   saved=(a.output/f'{code}/stats.json').read_bytes()
  finally:srv.shutdown();srv.server_close();thread.join()
  reloaded=b.BrokerState(a.output/f'{code}/stats.json',0,0)
  srv,thread=server(b.make_handler(reloaded,f'http://127.0.0.1:{up.server_port}/{code}',key))
  try:
   status,_=post(srv,str(code));assert status==409 and len(calls)==start+1
   assert saved==(a.output/f'{code}/stats.json').read_bytes() or reloaded.public()['requests']==stats['requests']
   assert reloaded.public()['requests'][0]['http_error_evidence']==meta
  finally:srv.shutdown();srv.server_close();thread.join()
  proofs.append({'status':code,'private_error_evidence':meta,'upstream_calls':1,'unknown_not_replayed':True,'unknown_preserved_on_reload':True,'credential_redacted':True,'private_body_not_in_lower_feedback':True})
finally:up.shutdown();up.server_close();t.join()
result={'valid':True,'external_api_calls':0,'actual_loopback_http_posts':len(calls),'controls':proofs,'source_sha256':hashlib.sha256(Path(b.__file__).read_bytes()).hexdigest()}
(a.output/'verification.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
