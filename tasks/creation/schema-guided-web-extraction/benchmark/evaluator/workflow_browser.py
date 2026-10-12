"""Independently replay supplied browser controls and expose actual state pixels."""
from pathlib import Path
import json
import hashlib
from trusted_evidence import browser


CONTROLS={'dev_001':('load-more',None),'dev_002':('more',None),'test_001':('next','mode'),'test_002':('more',None),'test_003':('next','all-periods'),'test_006':('next','annex')}


def verify(case,output,spec,truth,work):
    if not spec['workflow'].get('browser'):
        return {'valid':True,'evaluation_state':'scoreable','mode':'local_file','snapshots':[],'assertions_scope':'ordered static source verification is performed by case_truth and the structural validator'}
    steps=[];next_control,mode_control=CONTROLS[case.name];state_steps=truth['state_steps'];entry=state_steps[0]['target'].removeprefix('assets/site/')
    for step in state_steps[1:]:
        steps.append({'action':'click','target':next_control if step['action']=='load_more' else mode_control,'state':step['state'],'source_target':step['target']})
    for target in truth.get('targets',{}).get('visit_detail',[]):steps.append({'action':'navigate','target':target.removeprefix('assets/site/'),'source_target':target})
    for target in truth.get('targets',{}).get('ocr',[]):steps.append({'action':'navigate','target':target.removeprefix('assets/site/'),'source_target':target})
    root=case/'assets/site';files={p.relative_to(root).as_posix():p for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
    result=browser(files,entry,steps,work)
    if result.get('evaluation_state')=='infrastructure_error':return result
    snapshots=result.get('snapshots',[]);errors=result.setdefault('errors',[])
    states=snapshots[:len(state_steps)]
    if len(states)!=len(state_steps):errors.append('independent workflow did not produce every required state')
    if len({s['observed']['text'] for s in states})!=len(state_steps):errors.append('required control transitions did not change rendered state')
    result['state_bindings']=[{'required_state':step['state'],'observed_text':snap['observed']['text'],'image_sha256':snap['image']['sha256']} for step,snap in zip(state_steps,states)]
    rendered_state_hashes={snap['image']['sha256']:step['state'] for step,snap in zip(state_steps,states)}
    result['candidate_screenshots']=[]
    try:
        from PIL import Image,ImageStat
        import io
        def _decode_stats(raw):
            image=Image.open(io.BytesIO(raw));image.load()
            if image.width*image.height>20000000:raise ValueError('oversized screenshot')
            stats=ImageStat.Stat(image.convert('RGB'));return image.width,image.height,max(stats.var)
    except ImportError:
        # 0916 rejudge: the schema trusted runtime has no Pillow. Minimal pure-Python PNG decoder
        # (zlib + PNG unfiltering, 8-bit gray/RGB/RGBA, non-interlaced) providing the same three
        # facts the PIL path used: width, height and max per-channel pixel variance.
        import zlib,struct
        def _decode_stats(raw):
            if raw[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('not a PNG')
            pos=8;idat=[];w=h=None
            while pos<len(raw):
                ln,=struct.unpack('>I',raw[pos:pos+4]);typ=raw[pos+4:pos+8];data=raw[pos+8:pos+8+ln];pos+=12+ln
                if typ==b'IHDR':w,h,depth,ctype,_,_,interlace=struct.unpack('>IIBBBBB',data)
                elif typ==b'IDAT':idat.append(data)
                elif typ==b'IEND':break
            if w is None or depth!=8 or interlace!=0 or ctype not in (0,2,4,6):raise ValueError('unsupported PNG')
            if w*h>20000000:raise ValueError('oversized screenshot')
            ch={0:1,2:3,4:2,6:4}[ctype];stride=w*ch;body=zlib.decompress(b''.join(idat))
            prev=bytearray(stride);sums=[0.0]*3;sq=[0.0]*3;n=0;step=max(1,(w*h)//200000)
            for y in range(h):
                off=y*(stride+1);ft=body[off];line=bytearray(body[off+1:off+1+stride])
                for i in range(stride):
                    a=line[i-ch] if i>=ch else 0;b=prev[i];c=prev[i-ch] if i>=ch else 0
                    if ft==1:line[i]=(line[i]+a)&255
                    elif ft==2:line[i]=(line[i]+b)&255
                    elif ft==3:line[i]=(line[i]+((a+b)>>1))&255
                    elif ft==4:
                        p=a+b-c;pa=abs(p-a);pb=abs(p-b);pc=abs(p-c)
                        line[i]=(line[i]+(a if pa<=pb and pa<=pc else (b if pb<=pc else c)))&255
                for x in range(0,w,step):
                    px=line[x*ch:x*ch+ch]
                    rgb=(px[0],px[0],px[0]) if ch<3 else (px[0],px[1],px[2])
                    for k in range(3):sums[k]+=rgb[k];sq[k]+=rgb[k]*rgb[k]
                    n+=1
                prev=line
            var=max((sq[k]/n)-(sums[k]/n)**2 for k in range(3)) if n else 0.0
            return w,h,var
    try:trace=json.loads((output/'interaction_trace.json').read_text())
    except Exception:trace=[]
    for step,snap in zip(state_steps,states):
        candidates=[event for event in trace if isinstance(event,dict) and event.get('state')==step['state'] and isinstance(event.get('screenshot'),str)]
        if not candidates:errors.append('candidate missing screenshot for '+step['state']);continue
        path=(output/candidates[0]['screenshot']).resolve()
        if not path.is_relative_to(output.resolve()) or not path.is_file():errors.append('candidate screenshot missing/unsafe');continue
        raw=path.read_bytes()
        actual_hash=hashlib.sha256(raw).hexdigest()
        other_state=rendered_state_hashes.get(actual_hash)
        if other_state is not None and other_state!=step['state']:errors.append('candidate screenshot depicts another required state: '+step['state']+' -> '+other_state)
        import base64
        try:
            _w,_h,pixel_variance=_decode_stats(raw)
            if pixel_variance<1:errors.append('blank candidate screenshot: '+step['state'])
        except Exception as exc:errors.append('candidate screenshot cannot decode: '+step['state']);continue
        result['candidate_screenshots'].append({'state':step['state'],'dimensions':[_w,_h],'pixel_variance':pixel_variance,'image':{'mime':'image/png','sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode()},'comparison':'judge must compare this submitted capture to independent state pixels; different viewport/style is allowed'})
    result['valid']=not errors
    for index,snapshot in enumerate(snapshots):
        if index>len(state_steps) and snapshot['step'].get('target','').endswith(('.html','.json','.vcf')):
            snapshot['image_sha256']=snapshot.pop('image')['sha256']
    result['assertions_scope']='independent actual fixture actions/state/body/screenshots; submitted captures are separately labeled, not treated as a trusted execution log'
    return result
