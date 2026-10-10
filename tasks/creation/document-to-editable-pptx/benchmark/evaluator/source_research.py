"""Credential-free public source retrieval for independent deck claim review."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import time

class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0;self.metadata=[];self.jsonld=None
    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('type')=='application/ld+json':self.jsonld=''
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='meta':
            a=dict(attrs)
            if any(x in (a.get('name','')+' '+a.get('property','')).lower() for x in ('date','published','modified','title','citation')):self.metadata.append(a)
    def handle_endtag(self,tag):
        if tag=='script' and self.jsonld is not None:
            try:
                def walk(value):
                    if isinstance(value,dict):
                        for key,item in value.items():
                            if key in ('datePublished','dateModified'):self.metadata.append({'property':'article:published_time' if key=='datePublished' else 'article:modified_time','content':str(item),'origin':'JSON-LD'})
                            walk(item)
                    elif isinstance(value,list):
                        for item in value:walk(item)
                walk(json.loads(self.jsonld))
            except ValueError:pass
            self.jsonld=None
        if tag in ('script','style','noscript'):self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if self.jsonld is not None:self.jsonld+=data
        if not self.skip and data.strip():self.parts.append(data.strip())

def public_url(url):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('source URL must be public HTTP(S)')
    answers=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(a[4][0]).is_global for a in answers):raise ValueError('source URL resolves outside public network')
    return url

class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return super().redirect_request(req,fp,code,msg,headers,public_url(newurl))

def urls(value):
    if isinstance(value,str):
        yield from re.findall(r'https?://[^\s<>"\]\)]+',value)
    elif isinstance(value,dict):
        for item in value.values():yield from urls(item)
    elif isinstance(value,list):
        for item in value:yield from urls(item)

def temporal_review(request, record):
    """Explicitly separate access time, dated publication, and historical counts."""
    match=re.search(r'(?:on or before|historical date|dated daily list for)\s*\*?\*?(\d{4}-\d{2}-\d{2})',request,re.I)
    if not match:
        match=re.search(r'published on or before\s*\*?\*?(\d{4}-\d{2}-\d{2})',request,re.I)
    if not match:return {'required':False,'status':'not_required'}
    cutoff=match.group(1)
    publications=[];modifications=[]
    for meta in record.get('publication_metadata',[]):
        label=meta.get('property',meta.get('name','')).lower();date=re.search(r'\d{4}-\d{2}-\d{2}',meta.get('content',''))
        if date:
            if 'published' in label or 'publication' in label or 'citation_date' in label:publications.append(date.group())
            if 'modified' in label or 'updated' in label:modifications.append(date.group())
    capture=re.search(r'web\.archive\.org/web/(\d{8})',record.get('final_url',''))
    capture_date=(capture[1][:4]+'-'+capture[1][4:6]+'-'+capture[1][6:8]) if capture else None
    try:memento_date=parsedate_to_datetime(record.get('memento_datetime','')).date().isoformat()
    except (ValueError,TypeError):memento_date=None
    if not record.get('independently_retrieved'):
        status='unverified_unavailable_source'
    elif capture_date and capture_date==memento_date and capture_date<=cutoff:status='archived_body_at_or_before_cutoff'
    elif publications and min(publications)<=cutoff and (not modifications or max(modifications)<=cutoff):status='dated_publication_within_cutoff'
    elif publications and min(publications)>cutoff:status='published_after_cutoff_not_admissible'
    else:status='historical_body_unverified'
    engagement=bool(re.search(r'historical vote|upvotes|engagement',request,re.I))
    return {'required':True,'cutoff':cutoff,'status':status,'publication_dates':publications,'modification_dates':modifications,
        'archive_capture_date':capture_date,'memento_date':memento_date,'historical_engagement_verified':bool(engagement and status=='archived_body_at_or_before_cutoff'),
        'limitation':'Current cumulative votes and access timestamps do not establish historical votes. Publication metadata is source evidence, not proof that every live-page field existed then.'}

def collect(case_dir,candidate_output,evidence_root):
    request=(case_dir/'input.md').read_text()
    if 'closed-corpus' in request.lower():return {'mode':'closed-corpus','records':[],'retrieval_calls':0}
    manifest_path=candidate_output/'source_manifest.json'
    try:manifest=json.loads(manifest_path.read_text())
    except (OSError,ValueError):manifest={}
    seeds=list(dict.fromkeys(urls(manifest)))
    supplied=[]
    for path in [case_dir/'input.md',*sorted((case_dir/'assets').glob('*.md'))]:
        supplied.extend(urls(path.read_text()))
    seeds=list(dict.fromkeys(seeds+supplied))
    if len(seeds)>40:raise RuntimeError('independent source set exceeds review bound; no partial source score')
    root=evidence_root/'independent_sources';root.mkdir(exist_ok=True)
    records=[]
    opener=urllib.request.build_opener(PublicRedirect())
    for index,url in enumerate(seeds):
        record={'url':url,'accessed_at':datetime.now(timezone.utc).isoformat(),'independently_retrieved':False}
        try:
            public_url(url)
            request_obj=urllib.request.Request(url,headers={'User-Agent':'AgentSWE-Evaluator/1.0 (public-source-verification)'})
            # 0916 rejudge: retry rate-limited / transient upstream answers (429/5xx) with backoff
            # before treating a source as blocked; concurrent replays hit the same public hosts.
            response=None
            for _try in range(4):
                try:
                    response=opener.open(request_obj,timeout=45);break
                except urllib.error.HTTPError as _exc:
                    if _exc.code in (429,500,502,503,504) and _try<3:
                        time.sleep(15*(2**_try));continue
                    raise
            with response:
                body=response.read(12_000_001)
                if len(body)>12_000_000:raise RuntimeError('independent source too large; evidence incomplete')
                final=response.geturl();public_url(final)
                mime=response.headers.get('Content-Type','')
                record.update(status=response.status,final_url=final,content_type=mime,
                    last_modified=response.headers.get('Last-Modified'),http_date=response.headers.get('Date'),
                    memento_datetime=response.headers.get('Memento-Datetime'))
            suffix='.pdf' if body.startswith(b'%PDF-') else '.html'
            path=root/(str(index)+suffix);path.write_bytes(body)
            record.update(file=path.relative_to(evidence_root).as_posix(),sha256=hashlib.sha256(body).hexdigest(),independently_retrieved=True)
            if suffix=='.html':
                parser=Text();parser.feed(body.decode('utf-8',errors='replace'))
                record.update(text='\n'.join(parser.parts),publication_metadata=parser.metadata)
            else:
                import subprocess
                import os
                tool=Path(os.environ.get('AGENTSWE_TRUSTED_ENV_PREFIX','/opt/agentswe-trusted-runtime'))/'bin/pdftotext'
                cp=subprocess.run([str(tool),'-layout',str(path),'-'],capture_output=True,text=True,timeout=90)
                if cp.returncode:raise RuntimeError('independent public PDF reader failed')
                record['text']=cp.stdout
            record['historical_evidence']='archived_capture' if 'web.archive.org/web/' in url else 'live_source; verify publication metadata and historical cutoff in source text'
        except urllib.error.HTTPError as exc:
            # 0916 rejudge: after retries a 401/403/429/5xx source is recorded as blocked (no evidence for a claim)
            # instead of aborting the whole Eval; the judge sees the access status per source.
            record.update(status=exc.code,access_status='blocked_or_rate_limited_after_retries; not evidence for a claim')
            if exc.code in (401,403,429,500,502,503,504):record['infrastructure_http']=exc.code
            record.update(status=exc.code,access_status='blocked_or_missing; not evidence for a claim')
        except (urllib.error.URLError,TimeoutError,socket.gaierror) as exc:
            # 0916 rejudge: the evaluator host cannot reach every public host (no general egress).
            # Record the source as not independently retrievable instead of aborting the whole Eval;
            # the judge treats such a source as no evidence for a claim, exactly like a 404.
            record.update(access_status='network_unreachable_from_evaluator; not evidence for a claim',error=type(exc).__name__)
        except ValueError as exc:record.update(access_status='unsafe_source_not_fetched',error=str(exc))
        record['temporal_review']=temporal_review(request,record)
        records.append(record)
    infra=[r for r in records if r.get('infrastructure_http') or r.get('error') in ('URLError','TimeoutError','gaierror')]
    # 0916 rejudge: the evaluator host has no general egress and several public hosts bot-block it (e.g. nasa.gov -> 429 in 0.4 s).
    # Unreachable sources are reported to the judge as not independently verified; they never abort the Eval.
    return {'mode':'independent-public-retrieval','evaluator_unreachable_sources':len(infra),'evaluator_egress_note':'sources marked network_unreachable_from_evaluator or blocked_or_rate_limited_after_retries could not be fetched from the evaluator host; treat them as unverified leads, not as evidence against the deck' if infra else None,'retrieval_calls':len(records),'records':records,
        'instructions':'Source declarations are leads only. Check every material claim against these retrieved texts or page images, independently verify publication/capture dates against case cutoff, and disclose blocked or unsupported sources. HTTP access time never proves historical publication date.'}
