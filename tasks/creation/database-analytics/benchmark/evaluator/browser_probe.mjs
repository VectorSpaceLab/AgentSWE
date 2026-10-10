// Credential-free browser worker. The parent owns expectations and all assertions.
import {spawn,spawnSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {createHash} from 'node:crypto';

const plan=JSON.parse(readFileSync(process.argv[2],'utf8'));
const chrome=process.env.CHROMIUM;
const errors=[];const snapshots=[];const requests=[];
let browser,server,ws,profile,healthy=false;
const result={protocol:'trusted-browser-observation-v1',errors,snapshots,requests};
try {
  if(!chrome||spawnSync(chrome,['--version'],{encoding:'utf8'}).status!==0)throw Error('trusted Chromium missing');
  profile=mkdtempSync(path.join(tmpdir(),'data-web-chrome-'));
  browser=spawn(chrome,['--headless=new','--no-sandbox','--disable-gpu','--disable-background-networking','--disable-default-apps','--disable-extensions','--disable-sync','--no-first-run','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe'],env:Object.fromEntries(Object.entries(process.env).filter(([key])=>!/KEY|TOKEN|SECRET|PROXY|AUTH/i.test(key)))});
  const endpoint=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('trusted Chromium startup timeout')),15000);browser.stderr.on('data',data=>{text+=data;const m=text.match(/DevTools listening on (ws:\/\/\S+)/);if(m){clearTimeout(timer);resolve(m[1]);}});browser.on('exit',()=>reject(Error('trusted Chromium startup exit')));});
  ws=new WebSocket(endpoint);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  let sequence=0;const pending=new Map();let session;
  function call(method,params={},sessionId=session){return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},10000);pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value)},reject});ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});}
  ws.addEventListener('message',event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const p=pending.get(message.id);pending.delete(message.id);message.error?p.reject(Error(JSON.stringify(message.error))):p.resolve(message.result);}if(message.method==='Fetch.requestPaused'){const {requestId,request}=message.params;const permitted=request.url.startsWith(origin+'/')||/^(data:|about:|blob:)/.test(request.url);requests.push({url:request.url,permitted});call(permitted?'Fetch.continueRequest':'Fetch.failRequest',permitted?{requestId}:{requestId,errorReason:'BlockedByClient'},message.sessionId).catch(()=>{});}if(message.method==='Target.targetCreated'&&message.params.targetInfo.type==='page'&&message.params.targetInfo.targetId!==target.targetId)call('Target.closeTarget',{targetId:message.params.targetInfo.targetId},null).catch(()=>{});});
  const target=await call('Target.createTarget',{url:'about:blank'},null);session=(await call('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
  await call('Page.enable');await call('Runtime.enable');await call('Network.enable');await call('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  async function evaluate(expression){const tree=await call('Page.getFrameTree');const world=await call('Page.createIsolatedWorld',{frameId:tree.frameTree.frame.id,worldName:'trusted-inspection'});const value=await call('Runtime.evaluate',{expression,contextId:world.executionContextId,returnByValue:true,awaitPromise:true});if(value.exceptionDetails)throw Error('DOM observation failed');return value.result.value;}
  const health=await evaluate(`(()=>{const e=document.createElement('span');e.textContent='Visible ABC 123';e.style='font:16px sans-serif';document.body.append(e);const r=e.getBoundingClientRect();return {width:r.width,height:r.height}})()`);
  if(!(health.width>10&&health.height>5))throw Error('trusted browser text/font health failed');healthy=true;
  server=http.createServer((req,res)=>{try{const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);const rel=pathname.replace(/^\//,'');if(!Object.hasOwn(plan.files,rel)){res.writeHead(404);res.end('not found');return;}const file=plan.files[rel];res.writeHead(200,{'content-type':file.mime,'cache-control':'no-store'});res.end(Buffer.from(file.base64,'base64'));}catch{res.writeHead(500);res.end('trusted fixture server error');}});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const origin=`http://127.0.0.1:${server.address().port}`;
  await call('Fetch.enable',{patterns:[{urlPattern:'*'}]});await call('Target.setDiscoverTargets',{discover:true},null);
  const wait=()=>new Promise(resolve=>setTimeout(resolve,250));
  async function snapshot(step){await wait();const observed=await evaluate(`(()=>{const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&r.width>0&&r.height>0};return {url:location.pathname,text:document.body.innerText,rows:[...document.querySelectorAll('tr')].filter(visible).map(r=>[...r.querySelectorAll('th,td')].map(c=>c.innerText)),controls:[...document.querySelectorAll('select,input,button,[role=button]')].filter(visible).map(e=>({id:e.id,tag:e.tagName,value:e.value,text:e.innerText,options:e.options?[...e.options].map(o=>({value:o.value,text:o.text})):null})),images:[...document.images].map(e=>({src:e.getAttribute('src'),complete:e.complete,width:e.naturalWidth,height:e.naturalHeight}))}})()`);const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});snapshots.push({step,observed,image:{mime:'image/png',base64:shot.data,sha256:createHash('sha256').update(Buffer.from(shot.data,'base64')).digest('hex')}});return observed;}
  await call('Page.navigate',{url:origin+'/'+plan.entry});await wait();await snapshot({action:'open',target:plan.entry});
  if(plan.disable_javascript)await call('Emulation.setScriptExecutionDisabled',{value:true});
  for(const step of plan.steps){
    try{
      if(step.action==='navigate'){await call('Page.navigate',{url:origin+'/'+step.target});}
      else if(step.action==='select'){
        const control=await evaluate(`(()=>{const e=document.getElementById(${JSON.stringify(step.target)});return e&&{tag:e.tagName,options:e.options?[...e.options].map(x=>({value:x.value,text:x.text})):[]}})()`);
        if(!control)throw Error('missing control '+step.target);
        await evaluate(`(()=>{const e=document.getElementById(${JSON.stringify(step.target)});e.focus();e.value=${JSON.stringify(step.value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);
      }else if(step.action==='click'){
        const rect=await evaluate(`(()=>{const e=document.getElementById(${JSON.stringify(step.target)})||document.querySelector(${JSON.stringify(step.target)});if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height}})()`);
        if(!rect||rect.width<=0||rect.height<=0)throw Error('missing/invisible control '+step.target);
        await call('Input.dispatchMouseEvent',{type:'mousePressed',x:rect.x,y:rect.y,button:'left',clickCount:1});await call('Input.dispatchMouseEvent',{type:'mouseReleased',x:rect.x,y:rect.y,button:'left',clickCount:1});
      }else if(step.action==='inspect_controls'){
        const controls=snapshots[0].observed.controls;
        for(const c of controls){
          if(!c.id)continue;
          if(c.tag==='SELECT')for(const option of c.options){await evaluate(`(()=>{const e=document.getElementById(${JSON.stringify(c.id)});e.value=${JSON.stringify(option.value)};e.dispatchEvent(new Event('change',{bubbles:true}));return true})()`);await snapshot({action:'select',target:c.id,value:option.value,text:option.text});}
          else if(c.tag==='BUTTON'||c.tag==='INPUT'){await evaluate(`(()=>{document.getElementById(${JSON.stringify(c.id)}).click();return true})()`);await snapshot({action:'click',target:c.id});}
        }continue;
      }
      await snapshot(step);
    }catch(error){errors.push({step,error:String(error)});}
  }
  result.valid=errors.length===0;result.evaluation_state='scoreable';result.health=health;
  await call('Browser.close',{},null).catch(()=>{});
}catch(error){errors.push({error:String(error)});result.valid=false;result.evaluation_state=healthy?'scoreable':'infrastructure_error';}
finally{if(ws)ws.close();if(server)await new Promise(resolve=>server.close(resolve));if(browser)browser.kill('SIGTERM');if(profile){try{rmSync(profile,{recursive:true,force:true,maxRetries:10,retryDelay:200});}catch(cleanupError){}}}
console.log(JSON.stringify(result));
