"""Public-body verification in a credential-free host subprocess, before judging."""
from __future__ import annotations
import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import http.client
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import urllib.parse


class PageText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0;self.dates=[]
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','noscript'}:self.skip+=1
        attrs=dict(attrs)
        if tag=='meta' and any(x in attrs.get('property',attrs.get('name','')).lower() for x in ('published','modified','date')):self.dates.append(attrs.get('content',''))
        if tag=='time' and attrs.get('datetime'):self.dates.append(attrs['datetime'])
    def handle_endtag(self,tag):
        if tag in {'script','style','noscript'} and self.skip:self.skip-=1
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)


def fetch(url, address_attempt=0):
    hops=[]
    for _ in range(6):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,80,443):return {'state':'quality_error','error':'unauthorized source URL','hops':hops}
        host=parsed.hostname;port=parsed.port or (443 if parsed.scheme=='https' else 80)
        try:
            addresses=sorted({entry[4][0] for entry in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)})
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):return {'state':'quality_error','error':'nonpublic source destination','hops':hops}
            connection=http.client.HTTPSConnection(host,port,timeout=25,context=ssl.create_default_context()) if parsed.scheme=='https' else http.client.HTTPConnection(host,port,timeout=25)
            raw=None;last_error=None
            ordered=sorted(addresses,key=lambda address:ipaddress.ip_address(address).version)
            ordered=ordered[address_attempt:]+ordered[:address_attempt]
            for address in ordered[:3]:
                try:raw=socket.create_connection((address,port),timeout=8);connected=address;break
                except OSError as exc:last_error=exc
            if raw is None:raise last_error or OSError('no reachable pinned address')
            raw.settimeout(15)
            connection.sock=ssl.create_default_context().wrap_socket(raw,server_hostname=host) if parsed.scheme=='https' else raw
            connection.request('GET',urllib.parse.urlunsplit(('', '', parsed.path or '/',parsed.query,'')),headers={'Host':host,'User-Agent':'AgentSWE-Independent-Evaluator/1.0','Accept':'text/html,application/pdf,text/plain,*/*;q=0.1','Accept-Encoding':'identity'})
            response=connection.getresponse();headers=dict(response.getheaders());body=response.read(12*1024*1024+1);code=response.status;connection.close()
            hops.append({'url':url,'status':code,'resolved_addresses':addresses,'connected_address':connected,'headers':{k:v for k,v in headers.items() if k.lower() in {'content-type','last-modified','date','location','memento-datetime'}}})
            if code in (301,302,303,307,308):url=urllib.parse.urljoin(url,headers.get('Location',headers.get('location','')));continue
            if code in (404,410):return {'state':'quality_error','error':'source does not exist','hops':hops}
            if code>=400:return {'state':'infrastructure_error','error':'source access HTTP '+str(code),'hops':hops}
            if len(body)>12*1024*1024:return {'state':'infrastructure_error','error':'source exceeds verifier body limit','hops':hops}
            resource={}
            if host=='api.github.com' and '/contents/' in parsed.path:
                document=json.loads(body)
                if document.get('encoding')=='base64' and isinstance(document.get('content'),str):
                    decoded=base64.b64decode(document['content']);resource={'github_blob_sha':document.get('sha'),'github_api_response_sha256':hashlib.sha256(body).hexdigest(),'github_path':document.get('path')};body=decoded
            return {'state':'verified_body','hops':hops,'body':body,'final_url':url,'content_type':headers.get('Content-Type',headers.get('content-type','')),**resource}
        except socket.gaierror as exc:return {'state':'quality_error' if exc.errno==socket.EAI_NONAME else 'infrastructure_error','error':'DNS resolution '+str(exc.errno),'hops':hops}
        except (OSError,http.client.HTTPException) as exc:
            if address_attempt<2:
                retry=fetch(url,address_attempt+1);retry.setdefault('failed_address_attempts',[]).append({'attempt':address_attempt,'error':type(exc).__name__,'hops':hops});return retry
            return {'state':'infrastructure_error','error':type(exc).__name__,'hops':hops}
    return {'state':'quality_error','error':'redirect chain exceeds six hops','hops':hops}


# 0916 rejudge: a single source that the evaluator host cannot reach (no general egress) no longer voids the
# whole Eval; it stays listed in infrastructure_sources as not independently verified. Only a majority of
# unreachable sources is treated as evaluator infrastructure failure.
def verify(case,output,archive):
    archive.mkdir(parents=True,exist_ok=False)
    sources=json.loads((output/'sources.json').read_text());graph=json.loads((output/'evidence_graph.json').read_text());cutoff=sources.get('research_as_of');source_rows=sources.get('sources',[])
    if not isinstance(source_rows,list):source_rows=[]
    text=(case/'input.md').read_text();dates=re.findall(r'\b20\d\d-\d\d-\d\d\b',text);expected_cutoff=dates[0] if dates else cutoff
    results=[]
    def one(source):
        sid=source.get('id');locator=source.get('locator');result={'source_id':sid,'candidate_locator':locator,'retrieved_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        if not isinstance(sid,str) or not re.fullmatch(r'S[1-9][0-9]*',sid):return {**result,'state':'quality_error','error':'invalid source ID'}
        if source.get('access_depth')=='search_snippet':return {**result,'state':'unused_lead'}
        if source.get('kind')=='local':
            if not isinstance(locator,str):return {**result,'state':'quality_error','error':'invalid local locator'}
            path=(case/locator).resolve()
            if not path.is_relative_to((case/'assets').resolve()) or not path.is_file() or path.is_symlink():return {**result,'state':'quality_error','error':'unauthorized/missing local source'}
            observed={'state':'verified_body','body':path.read_bytes(),'hops':[],'content_type':'','final_url':locator}
        else:observed=fetch(locator) if isinstance(locator,str) else {'state':'quality_error','error':'invalid source locator'}
        body=observed.pop('body',None);result.update(observed)
        if body is None:return result
        result['sha256']=hashlib.sha256(body).hexdigest();file=archive/(sid+'.body');file.write_bytes(body);result['archive_file']=file.name;pages=[]
        if body.startswith(b'%PDF-'):
            try:
                executable=os.environ.get('AGENTSWE_PDF_READER_PYTHON','${AGENTSWE_HOME}/benchmark/envs/evidence-grounded-document-qa-agent-v2/bin/python')
                cp=subprocess.run([executable,'-I','-c','import fitz,sys;d=fitz.open(sys.argv[1]);print("\\f".join(p.get_text() for p in d))',str(file)],capture_output=True,text=True,timeout=30)
                if cp.returncode:raise RuntimeError('PDF extraction failed')
                pages=cp.stdout.split('\f');content=cp.stdout;result['page_count']=len([p for p in pages if p.strip()]);result['observed_dates']=[]
            except Exception as exc:return {**result,'state':'infrastructure_error','error':'independent PDF reader '+type(exc).__name__}
        else:
            parser=PageText();parser.feed(body.decode('utf-8',errors='replace'));content=' '.join(parser.parts);result['observed_dates']=parser.dates
        normalized=' '.join(content.split());result['body_text']=normalized[:70000];result['body_text_truncated']=len(normalized)>70000;result['text_sha256']=hashlib.sha256(normalized.encode()).hexdigest()
        if len(normalized)<120 and source.get('kind')!='local':result.update(state='infrastructure_error',error='source body empty or blocked')
        result['passages']=[]
        for passage in graph.get('passages',[]):
            if not isinstance(passage,dict) or passage.get('source_id')!=sid:continue
            quote=' '.join(str(passage.get('quote','')).split());loc=passage.get('locator',{});scope=normalized;page_number=None
            if isinstance(loc,dict) and loc.get('type')=='page':
                try:page_number=int(loc['value']);scope=' '.join(pages[page_number-1].split()) if 0<page_number<=len(pages) else ''
                except (TypeError,ValueError,KeyError):scope=''
            position=scope.find(quote) if quote else -1
            result['passages'].append({'passage_id':passage.get('id'),'exact_quote_found':position>=0,'locator':loc,'page_number':page_number,'quote':quote,'independently_retrieved_context':scope[max(0,position-300):position+len(quote)+300] if position>=0 else None})
        observed_dates=[d[:10] for d in result['observed_dates'] if re.match(r'20\d\d-\d\d-\d\d',d)]
        archive_match=re.search(r'/web/(\d{8})\d*/',str(locator));archive_day=dt.datetime.strptime(archive_match.group(1),'%Y%m%d').date().isoformat() if archive_match else None
        result['temporal_evidence']={'research_cutoff':expected_cutoff,'candidate_published_at':source.get('published_at'),'candidate_updated_at':source.get('updated_at'),'independently_observed_dates':observed_dates,'archive_capture_date':archive_day,'historical_coverage':'pre_cutoff_archive' if archive_day and archive_day<=expected_cutoff else 'post_cutoff_dates_require_claim_review' if any(d>expected_cutoff for d in observed_dates) else 'body_dates_available' if observed_dates else 'not_independently_dated','instruction':'Live retrieval does not prove the same text existed at the cutoff; evaluate dated policy/version passages, archive timestamps, and supersession directly.'}
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(one,[s for s in source_rows if isinstance(s,dict)]))
    infrastructure=[r['source_id'] for r in results if r['state']=='infrastructure_error'];quality=[r['source_id'] for r in results if r['state']=='quality_error' or any(not p['exact_quote_found'] for p in r.get('passages',[]))]
    value={'protocol':'independent-public-source-v1','case_id':case.name,'evaluation_state':'scoreable','evaluator_unreachable_sources':len(infrastructure),'evaluator_egress_note':('sources in infrastructure_sources could not be fetched from the evaluator host (no general egress / bot blocking); treat them as not independently verified, not as evidence against the report' if infrastructure else None),'sources':results,'infrastructure_sources':infrastructure,'quality_sources':quality,'cutoff_matches_request':cutoff==expected_cutoff,'candidate_source_claims_are_not_authoritative':True}
    (archive/'source_verification.json').write_text(json.dumps(value,indent=2))
    return value


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--case',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--archive',type=Path,required=True);args=parser.parse_args()
    result=verify(args.case,args.output,args.archive);print(json.dumps({'state':result['evaluation_state'],'sources':len(result['sources'])}));raise SystemExit(2 if result['evaluation_state']=='infrastructure_error' else 0)
