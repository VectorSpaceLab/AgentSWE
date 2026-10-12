"""Trusted initial app worlds and private independent browser assertions.

Only materialized per-case files enter Candidate-visible storage. This module
contains controlled positive/negative fixture variants for evaluator regression;
it must never be mounted into the lower-agent namespace.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

CASE_WORLD = {
    'dev_001': ('registration', True, 'public focused repair and durable signed-in state'),
    'dev_002': ('inventory', False, 'public target invalidation and cancellation'),
    'test_001': ('registration', True, 'failed-first actual repair, regression, durable proof'),
    'test_002': ('inventory', False, 'lost preview response and exact retry; one actual browser run'),
    'test_003': ('registration', False, 'stale generation and foreign ownership rejection around actual app tests'),
    'test_004': ('inventory', False, 'in-flight workspace/test drift and permanent proof invalidation'),
    'test_005': ('inventory', False, 'real completed browser result delayed across owner cancellation'),
    'test_006': ('registration', False, 'nonterminal real run interruption and cold persistent recovery'),
}

GOALS = {
    'registration': 'Register exactly once using a valid email and password; show the signed-in email on /dashboard; preserve the authenticated session on reload without re-registering; reject invalid email and a duplicate existing email visibly without a false signed-in state.',
    'inventory': 'Import CSV with quoted commas; identify duplicate SKU by original source row and import unique rows once; combine name and positive-quantity filters; export every filtered row rather than only the current two-row page; when export fails, show an error and do not claim export success.',
}

SERVER = r'''import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
const accounts = new Map(); const base = path.dirname(new URL(import.meta.url).pathname);
const server = http.createServer(async (req,res)=>{
  if(req.method==='POST' && req.url==='/register'){
    let body='';for await(const chunk of req)body+=chunk;
    let value;try{value=JSON.parse(body)}catch{res.writeHead(400);res.end('{}');return}
    if(!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value.email||'')){res.writeHead(400);res.end(JSON.stringify({error:'Invalid email'}));return}
    if(accounts.has(value.email)){res.writeHead(409);res.end(JSON.stringify({error:'Duplicate email'}));return}
    accounts.set(value.email,value.password);res.writeHead(201,{'content-type':'application/json'});res.end(JSON.stringify({email:value.email}));return;
  }
  if(req.url==='/export-failure'){res.writeHead(503);res.end('Export unavailable');return}
  const file=path.join(base,'index.html');res.writeHead(200,{'content-type':'text/html'});res.end(fs.readFileSync(file));
});
server.listen(0,'127.0.0.1',()=>process.stdout.write(JSON.stringify({listening:true,port:server.address().port})+'\n'));
'''

REGISTRATION = r'''<!doctype html><html><body><main id="app"></main><script>
const app=document.getElementById('app');
function render(){
  const current=localStorage.getItem('session.email');
  if(current){history.replaceState({},'', '/dashboard');app.innerHTML='<h1>Signed in</h1><p data-testid="account-email"></p><button id="out">Sign out</button>';app.querySelector('p').textContent=current;document.getElementById('out').onclick=()=>{localStorage.removeItem('session.email');render()};return}
  history.replaceState({},'', '/register');app.innerHTML='<h1>Register</h1><form novalidate><label>Email<input id="email" type="email"></label><label>Password<input id="password" type="password"></label><button>Register</button></form><p role="alert"></p>';
  app.querySelector('form').onsubmit=async(event)=>{event.preventDefault();const email=document.getElementById('email').value;const alert=app.querySelector('[role=alert]');
    if(!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)){alert.textContent='Invalid email';return}
    const response=await fetch('/register',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({email,password:document.getElementById('password').value})});const result=await response.json();
    if(!response.ok){alert.textContent=result.error;return}
    localStorage.setItem('SESSION_WRITE_KEY',result.email);history.replaceState({},'', '/dashboard');
    app.innerHTML='<h1>Signed in</h1><p data-testid="account-email"></p><button id="out">Sign out</button>';app.querySelector('p').textContent=result.email;document.getElementById('out').onclick=()=>{localStorage.removeItem('session.email');render()};
  };
}render();
</script></body></html>'''

INVENTORY = r'''<!doctype html><html><body><h1>Inventory</h1>
<label>CSV file<input id="file" type="file"></label><p id="issues"></p><button id="confirm">Confirm import</button>
<label>Name filter<input id="filter"></label><label>Positive quantity only<input id="positive" type="checkbox"></label>
<table><tbody id="rows"></tbody></table><button id="next">Next page</button><label>Simulate export failure<input id="fail" type="checkbox"></label><button id="export">Export filtered CSV</button><p role="status"></p><p role="alert"></p>
<script>
let rows=[], pending=[], page=0;
function csv(text){const out=[];let row=[],cell='',quoted=false;for(let i=0;i<text.length;i++){const c=text[i];if(c==='"'){if(quoted&&text[i+1]==='"'){cell+='"';i++}else quoted=!quoted}else if(c===','&&!quoted){row.push(cell);cell=''}else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&text[i+1]==='\n')i++;row.push(cell);if(row.some(Boolean))out.push(row);row=[];cell=''}else cell+=c}row.push(cell);if(row.some(Boolean))out.push(row);return out}
const selected=()=>rows.filter(r=>r.name.toLowerCase().includes(document.getElementById('filter').value.toLowerCase())&&(!document.getElementById('positive').checked||r.quantity>0));
function render(){document.getElementById('rows').innerHTML='';for(const r of selected().slice(page*2,page*2+2)){const tr=document.createElement('tr');for(const key of ['sku','name','quantity']){const td=document.createElement('td');td.textContent=String(r[key]);tr.append(td)}document.getElementById('rows').append(tr)}}
document.getElementById('file').onchange=async(event)=>{const all=csv(await event.target.files[0].text());const seen=new Set(rows.map(r=>r.sku));pending=[];const issues=[];all.slice(1).forEach((r,i)=>{if(seen.has(r[0])){issues.push('Row '+(i+2)+': duplicate SKU '+r[0]);return}seen.add(r[0]);pending.push({sku:r[0],name:r[1],quantity:Number(r[2])})});document.getElementById('issues').textContent=issues.join('; ')};
document.getElementById('confirm').onclick=()=>{rows.push(...pending);pending=[];page=0;render()};
for(const id of ['filter','positive'])document.getElementById(id).oninput=()=>{page=0;render()};document.getElementById('next').onclick=()=>{page++;render()};
document.getElementById('export').onclick=async()=>{const status=document.querySelector('[role=status]'),alert=document.querySelector('[role=alert]');status.textContent='';alert.textContent='';if(document.getElementById('fail').checked){const response=await fetch('/export-failure');if(!response.ok){alert.textContent='Export failed';return}}
 const quote=value=>'"'+String(value).replaceAll('"','""')+'"';const data='sku,name,quantity\n'+selected().map(r=>[r.sku,r.name,r.quantity].map(quote).join(',')).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([data],{type:'text/csv'}));a.download='filtered-inventory.csv';a.click();URL.revokeObjectURL(a.href);status.textContent='Export complete'};
</script></body></html>'''

REGISTRATION_TEST = r'''import { test, expect } from '@playwright/test';
test(TITLE, async ({page})=>{
 const requests=[];page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname==='/register')requests.push(r.url())});
 await page.goto('/register');await page.getByLabel('Email',{exact:true}).fill('not-an-email');await page.getByLabel('Password').fill('correct-horse');await page.getByRole('button',{name:'Register',exact:true}).click();await expect(page.getByRole('alert')).toHaveText('Invalid email');expect(requests).toHaveLength(0);
 await page.getByLabel('Email',{exact:true}).fill('new@example.test');await page.getByRole('button',{name:'Register',exact:true}).click();await expect(page).toHaveURL(/dashboard$/);await expect(page.getByTestId('account-email')).toHaveText('new@example.test');expect(requests).toHaveLength(1);
 await page.reload();await expect(page).toHaveURL(/dashboard$/);await expect(page.getByTestId('account-email')).toHaveText('new@example.test');expect(requests).toHaveLength(1);
 await page.getByRole('button',{name:'Sign out'}).click();await page.getByLabel('Email',{exact:true}).fill('new@example.test');await page.getByLabel('Password').fill('another-password');await page.getByRole('button',{name:'Register',exact:true}).click();await expect(page.getByRole('alert')).toHaveText('Duplicate email');await expect(page).toHaveURL(/register$/);expect(requests).toHaveLength(2);
});
'''

INVENTORY_TEST = r'''import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
test(TITLE, async ({page})=>{
 await page.goto('/inventory');await page.getByLabel('CSV file').setInputFiles({name:'items.csv',mimeType:'text/csv',buffer:Buffer.from('sku,name,quantity\nA1,"Widget, large",4\nA1,duplicate,2\nB2,Widget small,3\nC3,Widget zero,0\nD4,Widget medium,5\nE5,Other,9')});
 await expect(page.locator('#issues')).toHaveText('Row 3: duplicate SKU A1');await page.getByRole('button',{name:'Confirm import'}).click();await expect(page.locator('tbody tr')).toHaveCount(2);await expect(page.getByText('Widget, large',{exact:true})).toBeVisible();
 await page.getByLabel('Name filter').fill('Widget');await page.getByLabel('Positive quantity only').check();const event=page.waitForEvent('download');await page.getByRole('button',{name:'Export filtered CSV'}).click();const download=await event;const text=await fs.readFile(await download.path(),'utf8');expect(text).toContain('"A1","Widget, large","4"');expect(text).toContain('"B2","Widget small","3"');expect(text).toContain('"D4","Widget medium","5"');expect(text).not.toContain('"C3"');expect(text).not.toContain('"E5"');expect(text.split('\n')).toHaveLength(4);
 await page.getByLabel('Simulate export failure').check();await page.getByRole('button',{name:'Export filtered CSV'}).click();await expect(page.getByRole('alert')).toHaveText('Export failed');await expect(page.getByRole('status')).toBeEmpty();
});
'''


def materialize(case_id: str, destination: Path, *, positive_control: bool = False) -> dict:
    family, defective, axis = CASE_WORLD[case_id]
    destination.mkdir(parents=True, exist_ok=False)
    if family == 'registration':
        html = REGISTRATION.replace('SESSION_WRITE_KEY', 'session.email.stale' if defective and not positive_control else 'session.email')
        tests = REGISTRATION_TEST
    else:
        html, tests = INVENTORY, INVENTORY_TEST
    files = {'index.html': html, 'server.mjs': SERVER,
        'BUSINESS_REQUIREMENTS.md': GOALS[family] + '\n'}
    for name, value in files.items():
        (destination / name).write_text(value)
    test = tests.replace('TITLE', json.dumps(case_id + ' actual ' + family + ' behavior'))
    private_test = destination.parent / (destination.name + '-independent.spec.ts')
    private_test.write_text(test)
    return {'case_id': case_id, 'family': family, 'initially_defective': defective and not positive_control,
        'public_goal': GOALS[family], 'original_lifecycle_axis': axis, 'source_root': str(destination),
        'private_independent_test': str(private_test), 'private_test_sha256': hashlib.sha256(test.encode()).hexdigest(),
        'source_sha256': {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()},
        'fixture_prepares_business_state_not_acceptance_solution': True,
        'known_initial_defect': 'session is not restored after reload' if defective and not positive_control else None}
