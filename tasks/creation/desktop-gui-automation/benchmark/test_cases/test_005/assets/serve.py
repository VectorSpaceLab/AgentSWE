#!/usr/bin/env python3
import argparse,json,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
CASE_ID='test_005';ROOT=Path(__file__).resolve().parent;LOCK=threading.Lock();REC={'state':{},'trace':[],'decisive_snapshots':[],'record_count':0}
class H(BaseHTTPRequestHandler):
 def log_message(self,*_):pass
 def x(self,n,b,t='application/json'):
  if isinstance(b,(dict,list)):b=json.dumps(b).encode()
  elif isinstance(b,str):b=b.encode()
  self.send_response(n);self.send_header('Content-Type',t);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
  p=self.path.split('?',1)[0]
  if p not in ('/health', '/__evaluator__/state') and self.headers.get('Cookie', '') != f'evaluator_browser_session={self.server.browser_session}':return self.x(403,{'error':'browser session required'})
  if p=='/':self.x(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
  elif p=='/health':self.x(200,{'ready':True,'case_id':CASE_ID})
  elif p=='/__evaluator__/state':
   if self.headers.get('X-Evaluator-Token')!=self.server.token:return self.x(403,{'error':'forbidden'})
   with LOCK:d=json.loads(json.dumps(REC));d['case_id']=CASE_ID;self.x(200,d)
  else:self.x(404,{'error':'not found'})
 def do_POST(self):
  try:d=json.loads(self.rfile.read(min(int(self.headers.get('Content-Length','0')),2000000)) or b'{}')
  except:return self.x(400,{'error':'invalid json'})
  if self.path!='/record':return self.x(404,{'error':'not found'})
  if self.headers.get('Cookie', '') != f'evaluator_browser_session={self.server.browser_session}':return self.x(403,{'error':'browser session required'})
  with LOCK:
   for k in ('state','trace','decisive_snapshots'):REC[k]=d.get(k,REC[k])
   REC['record_count']+=1
  self.x(200,{'ok':True})
def main():
 p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=0);p.add_argument('--evaluator-token',required=True);p.add_argument('--browser-session',required=True);a=p.parse_args();s=ThreadingHTTPServer((a.host,a.port),H);s.token=a.evaluator_token;s.browser_session=a.browser_session;print(json.dumps({'ready':True,'case_id':CASE_ID,'url':f'http://{a.host}:{s.server_port}'}),flush=True);s.serve_forever()
if __name__=='__main__':main()
