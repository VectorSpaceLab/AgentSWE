#!/usr/bin/env python3
import argparse, json, subprocess, sys
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--case-dir',type=Path,required=True); p.add_argument('--output-dir',type=Path,required=True); p.add_argument('--result',type=Path,required=True); a=p.parse_args()
    render_dir = a.result.parent / 'render'
    render_dir.mkdir(parents=True, exist_ok=True)
    render_report = render_dir / 'render_report.json'
    render = subprocess.run([
        sys.executable, '/evaluator/render_pptx.py',
        str(a.output_dir / 'deck.pptx'), str(render_dir),
        '--report', str(render_report),
        '--libreoffice-root', '/tools/libreoffice/root',
        '--pdftoppm', '/opt/agentswe-trusted-runtime/bin/pdftoppm',
    ], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (a.result.parent / 'render.stdout.log').write_text(render.stdout, encoding='utf-8')
    try:
        report=json.loads(render_report.read_text())
        errors=report['errors']
        if not isinstance(errors,list): raise ValueError('invalid renderer errors')
        if any(str(e).startswith(('missing evaluator ', 'render directory is not empty:')) for e in errors):
            raise ValueError('trusted renderer runtime/isolation failure')
        if any(c.get('exit_code') == 127 or 'error while loading shared libraries' in str(c.get('stderr','')) for c in report.get('commands',[])):
            raise ValueError('trusted renderer dependency loader failure')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        a.result.write_text(json.dumps({'case':a.case_dir.name,'contract_valid':False,
            'validity_gate':False,'infrastructure_error':str(exc)})+'\n')
        raise RuntimeError('PPTX trusted renderer failed; do not judge this case') from exc
    cmd=[sys.executable,'/evaluator/validate_pptx.py','--case-id',a.case_dir.name,'--output-dir',str(a.output_dir),'--expectations','/evaluator/case_expectations.json','--report',str(a.result),'--render-dir',str(render_dir)]
    r=subprocess.run(cmd,check=False)
    value=json.loads(a.result.read_text()) if a.result.is_file() else {'valid':False,'errors':['missing result']}
    value['case']=a.case_dir.name
    value['render_exit_code']=render.returncode
    value['render_report']=str(render_report)
    value['validity_gate']=bool(value.get('valid')) and r.returncode==0 and render.returncode==0
    a.result.write_text(json.dumps(value,indent=2)+'\n')

if __name__=='__main__': main()
