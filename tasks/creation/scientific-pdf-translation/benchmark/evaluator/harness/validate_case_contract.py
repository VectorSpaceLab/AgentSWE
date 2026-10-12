#!/usr/bin/env python3
"""PDF core validity, every-page scientific evidence, and offline viewer behavior."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import uuid

def module(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case-id',required=True)
    for name in ('case-dir','output-dir','report'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    result={'case':args.case_id,'case_id':args.case_id,'valid':False,'validity_gate':False,'fatal_gate':False,
        'evaluation_state':'infrastructure_error','errors':[],'quality_errors':[]}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    try:
        import fitz
        reader=module('evidence_bundle')
        health=fitz.open();page=health.new_page();page.insert_text((40,40),'PDF health 731')
        if '731' not in page.get_text() or not page.get_pixmap().samples:raise RuntimeError('trusted PDF parser/renderer health failed')
        health.close()
        request=(args.case_dir/'input.md').read_text()
        paths=re.findall(r'\]\(([^)]+\.pdf)\)',request,re.I)
        if not paths:raise RuntimeError('active request has no PDF sources')
        sources=[]
        for name in paths:
            path=(args.case_dir/name).resolve()
            if not path.is_relative_to(args.case_dir.resolve()) or not path.is_file():raise RuntimeError('active case PDF missing/unsafe')
            sources.append(path)
        target=args.output_dir/'translated.pdf'
        if not target.is_file():
            result.update(evaluation_state='fatal_zero',fatal_gate=True,errors=['missing translated.pdf'])
        else:
            try:
                document=fitz.open(target)
                if document.is_encrypted or not len(document):raise ValueError('encrypted or empty PDF')
                for page in document:page.get_pixmap(matrix=fitz.Matrix(.25,.25))
                document.close()
            except Exception as exc:
                result.update(evaluation_state='fatal_zero',fatal_gate=True,errors=['unparseable/unrenderable translated.pdf: '+type(exc).__name__])
        if not result['fatal_gate']:
            root=args.report.parent/('pdf-evidence-'+uuid.uuid4().hex)
            root.mkdir()
            source_pages,images=[],[]
            for index,path in enumerate(sources,1):
                pages,pictures=reader.extract_pdf(path,root,'source',len(source_pages),'source-'+str(index))
                source_pages.extend(pages);images.extend(pictures)
            target_pages,pictures=reader.extract_pdf(target,root,'target');images.extend(pictures)
            alignment=reader.check_alignment(args.output_dir/'alignment.json',sources,target)
            if (args.output_dir/'viewer/index.html').is_file():
                viewer=module('browser_probe').probe(args.output_dir,root,alignment,{'source':len(source_pages),'target':len(target_pages)})
            else:viewer={'functional':False,'errors':['missing viewer/index.html'],'screenshots':[],'actions':[]}
            for filename in viewer.get('screenshots',[]):images.append({'role':'viewer_screenshot','file':filename,'sha256':reader.file_hash(root/filename)})
            copy_checks={}
            for side,pages in (('source',source_pages),('translated',target_pages)):
                copy=args.output_dir/'viewer/documents'/(side+'.pdf')
                try:
                    d=fitz.open(copy)
                    actual=[(reader.text_digest(p.get_text()), p.rect.width, p.rect.height, p.rotation,
                        hashlib.sha256(p.get_pixmap(matrix=fitz.Matrix(.7,.7),alpha=False).samples).hexdigest()) for p in d]
                    expected=[]
                    for path in sources if side=='source' else [target]:
                        with fitz.open(path) as original:expected.extend((reader.text_digest(p.get_text()),p.rect.width,p.rect.height,p.rotation,
                            hashlib.sha256(p.get_pixmap(matrix=fitz.Matrix(.7,.7),alpha=False).samples).hexdigest()) for p in original)
                    copy_checks[side]={'matches':actual==expected,'page_count':len(d)}
                    d.close()
                except Exception as exc:copy_checks[side]={'matches':False,'error':type(exc).__name__}
            if len(source_pages)!=len(target_pages):result['quality_errors'].append('source/target page count mismatch')
            if not alignment['functional']:result['quality_errors'].append('alignment defects')
            if not viewer['functional']:result['quality_errors'].append('viewer defects')
            if not all(c['matches'] for c in copy_checks.values()):result['quality_errors'].append('viewer PDF copies mismatch')
            report=args.output_dir/'run_report.json'
            try:
                report_value=json.loads(report.read_text())
                if not isinstance(report_value,dict):raise ValueError('report not an object')
            except Exception as exc:report_value={'quality_error':type(exc).__name__};result['quality_errors'].append('run report missing/malformed')
            bundle={'case':args.case_id,'case_digest':reader.tree_digest(args.case_dir),'output_digest':reader.tree_digest(args.output_dir),
                'source_pages':source_pages,'target_pages':target_pages,'images':images,'alignment':alignment,'viewer':viewer,
                'viewer_copies':copy_checks,'run_report':report_value,'quality_ceilings':reader.CEILINGS,
                'sources':[{'source':p.relative_to(args.case_dir).as_posix(),'sha256':reader.file_hash(p),'text':p.read_text()} for p in args.case_dir.rglob('*') if p.is_file() and p.suffix in ('.md','.txt','.csv')]}
            result.update(valid=True,validity_gate=True,evaluation_state='scoreable',visual_evidence=reader.seal(root,bundle),
                source_page_total=len(source_pages),target_page_total=len(target_pages),alignment_functional=alignment['functional'],viewer_functional=viewer['functional'])
    except Exception as exc:
        result.update(evaluation_state='infrastructure_error',valid=False,validity_gate=False,fatal_gate=False,
            infrastructure_error=type(exc).__name__+': '+str(exc))
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return 70 if result['evaluation_state']=='infrastructure_error' else 0

if __name__=='__main__':raise SystemExit(main())
