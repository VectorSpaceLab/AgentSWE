#!/usr/bin/env python3
"""Exercise submitted PDF viewer only in the offline verifier environment."""
from __future__ import annotations
import functools
import base64
import http.server
import json
import os
import threading


def probe(output, evidence_root, alignment, page_counts):
    from playwright.sync_api import sync_playwright
    result = {'functional': False, 'errors': [], 'external_requests': [], 'actions': [], 'viewports': [], 'screenshots': []}
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get('CHROMIUM'), headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage'])
            context = browser.new_context(viewport={'width':1440,'height':900}, service_workers='block')
            health = context.new_page()
            health.set_content('<p style="font:24px sans-serif">PDF evaluator health 731</p>')
            if health.locator('p').bounding_box()['height'] < 10:
                raise RuntimeError('trusted browser font/text health failed')
            health.close()
            page = context.new_page()
            def route(request_route):
                url = request_route.request.url
                if url.startswith(origin + '/'):
                    request_route.continue_()
                else:
                    result['external_requests'].append(url)
                    request_route.abort()
            context.route('**/*', route)
            page.on('pageerror', lambda e: result['errors'].append(str(e)))
            try:
                page.goto(origin + '/viewer/index.html', wait_until='networkidle', timeout=30000)
                page.wait_for_function("document.querySelectorAll('.pdf-page canvas').length > 0 && document.querySelectorAll('.paragraph-overlay').length > 0", timeout=15000)
                page.wait_for_timeout(1000)
                for width, height in ((1440,900),(1280,800)):
                    page.set_viewport_size({'width':width,'height':height})
                    page.wait_for_timeout(200)
                    snapshot = page.evaluate("""() => ({
                      panes:['source','target'].map(s=>{let e=document.querySelector('#'+s+'-pane');return e?{side:s,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight}:null}),
                      pages:[...document.querySelectorAll('.pdf-page')].map(e=>({side:e.dataset.side,page:Number(e.dataset.pageNumber),canvas:!!e.querySelector('canvas'),width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height})),
                      overlays:[...document.querySelectorAll('.paragraph-overlay')].map(e=>{let r=e.getBoundingClientRect(),p=e.closest('.pdf-page').getBoundingClientRect();return {id:e.dataset.alignmentId,side:e.dataset.side,page:Number(e.dataset.pageNumber),bbox:[(r.x-p.x)/p.width,(r.y-p.y)/p.height,r.width/p.width,r.height/p.height],role:e.getAttribute('role'),tabindex:e.getAttribute('tabindex'),label:e.getAttribute('aria-label')};}),
                      canvases:[...document.querySelectorAll('.pdf-page canvas')].map(e=>{let a=e.getContext('2d').getImageData(0,0,e.width,e.height).data,n=0;for(let i=0;i<a.length;i+=64)if(Math.min(a[i],a[i+1],a[i+2])<230)n++;return {width:e.width,height:e.height,nonwhite:n};})
                    })""")
                    expected=[]
                    for record in alignment['records']:
                        for side in ('source','target'):
                            for anchor in record.get(side,{}).get('anchors',[]):
                                expected.append((str(record['id']),side,anchor['page'],anchor['bbox']))
                    actual=snapshot['overlays']
                    actual_pages=[(p['side'],p['page']) for p in snapshot['pages']]
                    expected_pages=[(side,n) for side in ('source','target') for n in range(1,page_counts[side]+1)]
                    if sorted(actual_pages)!=sorted(expected_pages):result['errors'].append('viewer page set differs from complete source/translation PDFs')
                    if len(actual)!=len(expected): result['errors'].append('overlay set does not equal all alignment anchors')
                    unmatched=list(actual)
                    for rid,side,number,box in expected:
                        found=next((e for e in unmatched if e['id']==rid and e['side']==side and e['page']==number and all(abs(a-b)<.012 for a,b in zip(e['bbox'],box))),None)
                        if not found: result['errors'].append(f'geometry missing/misaligned: {rid}/{side}/{number}')
                        else:
                            unmatched.remove(found)
                            if found['role']!='button' or found['tabindex']!='0' or not found['label']: result['errors'].append('inaccessible overlay: '+rid)
                    if any(p is None for p in snapshot['panes']): result['errors'].append('missing scroll pane')
                    if any(c['nonwhite']<20 or c['width']<100 for c in snapshot['canvases']): result['errors'].append('blank or undersized PDF canvas')
                    result['viewports'].append({'width':width,'height':height,**snapshot})
                    screenshot=evidence_root / f'viewer-{width}.png'
                    page.screenshot(path=str(screenshot))
                    result['screenshots'].append(screenshot.name)
                for index,record in enumerate(alignment['records']):
                    rid=str(record['id'])
                    for side in ('source','target'):
                        anchors=record.get(side,{}).get('anchors',[])
                        if not anchors: continue
                        opposite='target' if side=='source' else 'source'
                        methods=('click','Enter','Space') if index in (0,len(alignment['records'])-1) or len(anchors)>1 else ('click',)
                        for method in methods:
                            selector='.paragraph-overlay[data-side='+json.dumps(side)+'][data-alignment-id='+json.dumps(rid)+']'
                            item=page.locator(selector).first
                            page.locator('#'+opposite+'-pane').evaluate('e=>e.scrollTop=0')
                            item.scroll_into_view_if_needed(timeout=3000)
                            if method=='click': item.click(timeout=3000)
                            else: item.press(method)
                            page.wait_for_timeout(40)
                            observed=page.evaluate("""({id,opposite})=>{
                              const same=[...document.querySelectorAll('.paragraph-overlay')].filter(e=>e.dataset.alignmentId===id);
                              const other=[...document.querySelectorAll('.paragraph-overlay.is-active')].filter(e=>e.dataset.alignmentId!==id);
                              const target=same.find(e=>e.dataset.side===opposite),pane=document.querySelector('#'+opposite+'-pane').getBoundingClientRect();
                              let visible=null;if(target){let b=target.getBoundingClientRect();visible=Math.max(0,Math.min(b.bottom,pane.bottom)-Math.max(b.top,pane.top))/Math.min(b.height,pane.height)>.5;}
                              const styles=same.map(e=>{let s=getComputedStyle(e);let alpha=s.backgroundColor.match(/[\d.]+/g)||[];let fill=s.backgroundColor!=='transparent'&&(alpha.length<4||Number(alpha[3])>0);let outline=s.outlineStyle!=='none'&&parseFloat(s.outlineWidth)>0;return {background:s.backgroundColor,outline:s.outline,opacity:s.opacity,visible_highlight:Number(s.opacity)>0&&(fill||outline||s.boxShadow!=='none')};});
                              return {all_active:same.every(e=>e.classList.contains('is-active')&&e.getAttribute('aria-current')==='true'),old_cleared:other.length===0,counterpart_visible:visible,opposite_scroll:document.querySelector('#'+opposite+'-pane').scrollTop,styles,confidence:document.querySelector('[data-alignment-confidence]')?.textContent||''};
                            }""",{'id':rid,'opposite':opposite})
                            okay=observed['all_active'] and observed['old_cleared'] and (observed['counterpart_visible'] is not False) and all(s['visible_highlight'] for s in observed['styles'])
                            if record.get('status') in ('low_confidence','unresolved'):
                                okay=okay and record['status'] in observed['confidence']
                            if not record.get(opposite,{}).get('anchors'):
                                okay=okay and observed['opposite_scroll']==0
                            if not okay: result['errors'].append(f'activation failure: {rid}/{side}/{method}')
                            result['actions'].append({'id':rid,'side':side,'method':method,'passed':okay,**observed})
                from PIL import Image, ImageChops, ImageStat
                result['page_pixel_comparisons']=[]
                for rendered in page.locator('.pdf-page').all():
                    side=rendered.get_attribute('data-side');number=int(rendered.get_attribute('data-page-number'))
                    canvas=rendered.locator('canvas').first
                    encoded=canvas.evaluate('e=>e.toDataURL("image/png").split(",")[1]')
                    path=evidence_root/f'viewer-{side}-{number:03d}.png';path.write_bytes(base64.b64decode(encoded))
                    expected=evidence_root/f'{side}-{number:03d}.png'
                    with Image.open(path) as observed,Image.open(expected) as reference:
                        left=observed.convert('RGB').resize((420,594));right=reference.convert('RGB').resize((420,594))
                        difference=sum(ImageStat.Stat(ImageChops.difference(left,right)).mean)/3
                    result['page_pixel_comparisons'].append({'side':side,'page':number,'pixel_mean_absolute_error':difference,'file':path.name})
                    if difference>35:result['errors'].append(f'viewer canvas differs materially from independent PDF render: {side}/{number}')
                screenshot=evidence_root/'viewer-active.png';page.screenshot(path=str(screenshot));result['screenshots'].append(screenshot.name)
                result['functional']=not result['errors'] and not result['external_requests'] and bool(result['actions'])
            except Exception as exc:
                result['errors'].append(type(exc).__name__+': '+str(exc))
            browser.close()
    finally:
        server.shutdown();server.server_close();worker.join(timeout=3)
    return result
