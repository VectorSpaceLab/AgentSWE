#!/usr/bin/env python3
"""Minimal deterministic artifact-producing QA agent for adapter smoke tests."""
import argparse, base64, hashlib, json, html
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',required=True); p.add_argument('--output',required=True); a=p.parse_args()
    inp=Path(a.input).resolve(); case=inp.parent; out=Path(a.output).resolve(); out.mkdir(parents=True,exist_ok=True)
    assets=case/'assets'; claims=[]; bundle=[]; et={}; ct={}
    for i,src in enumerate(sorted(assets.iterdir())):
        if not src.is_file(): continue
        raw=src.read_bytes(); digest=hashlib.sha256(raw).hexdigest(); sid=src.name
        bundle.append({'source_id':sid,'mime_type':'application/octet-stream','sha256':digest,'encoding':'base64','bytes_base64':base64.b64encode(raw).decode()})
        eid=f'evidence-{i+1}'; cid=f'claim-{i+1}'
        if src.suffix.lower()=='.svg': native={'kind':'svg_rect','element_id':'root','x':0.0,'y':0.0,'width':1.0,'height':1.0}
        elif src.suffix.lower()=='.pdf': native={'kind':'pdf_rect','page':1,'x':0.0,'y':0.0,'width':1.0,'height':1.0}
        elif src.suffix.lower()=='.docx': native={'kind':'docx_cell','paragraph':0,'table':0,'row':0,'column':0}
        else: native={'kind':'byte_range','byte_start':0,'byte_end':max(1,min(len(raw),80))}
        et[eid]={'source_id':sid,'source_sha256':digest,'native_locator':native,'claim_ids':[cid],'locator':f'{sid} native source'}
        ct[cid]={'evidence_ids':[eid]}
        claims.append({'claim':f'Source {sid} is available for review.','status':'supported','confidence':'low','supporting_evidence':[{'source':sid,'locator':f'{sid} native source','quote_or_observation':'','relation':'supports'}],'contradicting_evidence':[]})
    (out/'answer.md').write_text('# Evidence-grounded answer\n\nThe supplied packet was reviewed; claims are listed with source-native links.\n',encoding='utf-8')
    (out/'claims_and_citations.json').write_text(json.dumps({'claims':claims},indent=2),encoding='utf-8')
    (out/'source_bundle.json').write_text(json.dumps({'sources':bundle},indent=2),encoding='utf-8')
    manifest={'schema_version':'1.0','claim_targets':ct,'evidence_targets':et}
    (out/'review_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    payload=json.dumps(bundle,separators=(',',':'))
    mjson=json.dumps(manifest,separators=(',',':'))
    page='''<!doctype html><meta charset="utf-8"><title>Offline Review</title><style>.sel{outline:2px solid #06c}</style><main id="app"></main><script>const BUNDLE=%s,MANIFEST=%s;let state={claimId:null,evidenceId:null,sourceId:null,sourceSha256:null};function render(){document.getElementById('app').textContent=state.claimId||state.evidenceId||'Offline source-native review';location.hash=state.claimId||state.evidenceId||'';}window.__reviewTestApi={selectClaim(id){state.claimId=id;state.evidenceId=(MANIFEST.claim_targets[id]||{}).evidence_ids?.[0]||null;let e=MANIFEST.evidence_targets[state.evidenceId]||{};state.sourceId=e.source_id||null;state.sourceSha256=e.source_sha256||null;render();return {...state}},selectEvidence(id){state.evidenceId=id;state.claimId=(MANIFEST.evidence_targets[id]||{}).claim_ids?.[0]||null;let e=MANIFEST.evidence_targets[id]||{};state.sourceId=e.source_id||null;state.sourceSha256=e.source_sha256||null;render();return {...state}},getState(){return {...state}}};render();</script>'''%(payload,mjson)
    (out/'review.html').write_text(page,encoding='utf-8')
    (out/'run_report.json').write_text(json.dumps({'status':'ok','artifact_paths':{k:k for k in ('answer.md','claims_and_citations.json','review.html','review_manifest.json','source_bundle.json','run_report.json')},'errors':[],'llm_requests':0,'gateway_image_requests':0,'serper_requests':0,'web_retrieval':0,'elapsed_seconds':0},indent=2),encoding='utf-8')
if __name__=='__main__': main()
