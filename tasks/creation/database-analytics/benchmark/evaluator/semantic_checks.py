"""Independent, asset-derived business calculations; no submitted SQL is used."""
from __future__ import annotations

from collections import defaultdict
import csv
import datetime as dt
import hashlib
import math
from pathlib import Path
import sqlite3


FORBIDDEN = {'account_contacts', 'worker_identity', 'compensation', 'driver_accounts', 'recipient_directory'}


def latest(rows, key, predicate=lambda row: True):
    result = {}
    for row in rows:
        if predicate(row) and (row[key] not in result or row['revision'] > result[row[key]]['revision']):
            result[row[key]] = row
    return list(result.values())


def moment(text):
    parsed=dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def inside(value, start, end):
    return value >= start and (end is None or value < end)


def calculate(case_input):
    case = case_input.parent
    database = next((case / 'assets').glob('*.sqlite'))
    con = sqlite3.connect(f'file:{database}?mode=ro&immutable=1', uri=True)
    con.row_factory = sqlite3.Row
    checks = {}
    def rows(table, columns='*', where='', params=()):
        if table in FORBIDDEN:
            raise ValueError('forbidden evaluator query')
        return [dict(row) for row in con.execute(f'SELECT {columns} FROM {table} ' + where, params)]
    def rev(table, key, predicate=lambda row: True, columns='*', where='', params=()):
        raw = rows(table, columns, where, params)
        kept = latest(raw, key, predicate)
        checks[table] = {'raw_rows': len(raw), 'retained_rows': len(kept)}
        return kept
    def fx(rate_set='board_2026_07_locked'):
        return {r['currency_code']: r for r in rows('fx_rates', where='WHERE rate_set = ?', params=(rate_set,))}
    def money(amount, code, rates, major=False):
        rate = rates[code]
        return amount * rate['usd_per_major'] / (1 if major else 10 ** rate['minor_digits'])
    out = []; hidden = {'primary': [], 'complementary': []}; status = 'answered'; decisions = {}
    cid = case.name
    try:
        if cid == 'dev_001':
            rates = fx(); stores = {r['store_id']: r for r in rows('stores')}
            orders = {r['order_id']: r for r in rows('orders') if r['order_status'] in {'paid', 'fulfilled'} and not r['is_test']}
            lines = rev('order_line_versions', 'line_key'); refunds = rev('refund_versions', 'refund_key', lambda r: r['recorded_at_utc'] <= '2026-07-15T23:59:59Z')
            refund_sum = defaultdict(float)
            for r in refunds:
                if r['refund_status'] == 'posted' and r['occurred_at_utc'] <= '2026-07-15T23:59:59Z': refund_sum[r['line_key']] += money(r['refund_amount_minor'], r['currency_code'], rates)
            groups = defaultdict(lambda: [0.0, 0.0]); shifted = 0
            for line in lines:
                order = orders.get(line['order_id'])
                if not order: continue
                store = stores[order['store_id']]; day = (moment(order['placed_at_utc']) + dt.timedelta(minutes=store['utc_offset_minutes'])).date().isoformat()
                shifted += day != order['placed_at_utc'][:10]
                period = 0 if '2025-04-01' <= day <= '2025-06-30' else 1 if '2026-04-01' <= day <= '2026-06-30' else None
                if period is not None: groups[(store['market'], line['product_category'])][period] += money(line['net_amount_minor'], line['currency_code'], rates) - refund_sum[line['line_key']]
            totals = defaultdict(float)
            for (market, category), value in groups.items(): totals[market] += value[1] - value[0]
            selected = sorted((m for m in totals if totals[m] < 0), key=lambda m: (totals[m], m))[:2]
            for market in selected:
                entries = sorted(((cat, v) for (m, cat), v in groups.items() if m == market), key=lambda item: (item[1][1] - item[1][0], item[0]))
                for category, value in entries: out.append(dict(market=market, product_category=category, q2_2025_usd=round(value[0], 2), q2_2026_usd=round(value[1], 2), change_usd=round(value[1] - value[0], 2)))
                decisions[market] = {'change_usd': round(totals[market], 2), 'largest_negative_category': entries[0][0]}
            checks.update(local_date_shifted_lines=shifted, eligible_orders=len(orders), selected_definition='Recognized net sales v2', fx_rate_set='board_2026_07_locked')
        elif cid == 'dev_002':
            orgs = {r['organization_id']: r for r in rows('organizations', 'organization_id, customer_tier, lifecycle_status, is_test', "WHERE region = 'West'") if r['lifecycle_status'] == 'customer' and not r['is_test']}
            subscriptions = rev('subscription_versions', 'subscription_key', columns='*', where="WHERE organization_id IN (SELECT organization_id FROM organizations WHERE region = 'West')")
            events = rev('activity_event_versions', 'event_uuid', where="WHERE organization_id IN (SELECT organization_id FROM organizations WHERE region = 'West')")
            days = defaultdict(set); families = defaultdict(set)
            for event in events:
                if not event['is_automated'] and event['event_type'] in {'analysis_run', 'dashboard_publish'} and '2026-05-17' <= event['activity_date_local'] <= '2026-06-30':
                    days[event['organization_id']].add(event['activity_date_local']); families[event['organization_id']].add(event['feature_family'])
            cells = defaultdict(lambda: [set(), set()])
            for sub in subscriptions:
                oid = sub['organization_id']
                if oid in orgs and sub['subscription_status'] == 'active' and inside('2026-06-30', sub['effective_from'], sub['effective_to']):
                    group = cells[(sub['plan'], orgs[oid]['customer_tier'])]; group[0].add(oid)
                    if len(days[oid]) >= 4 and {'analysis', 'publishing'} <= families[oid]: group[1].add(oid)
            counts = {key: [len(a), len(b)] for key, (a, b) in cells.items()}
            for plan in sorted({key[0] for key in counts}):
                primary = [tier for p, tier in counts if p == plan and counts[(p, tier)][0] < 8]
                complement = []
                if len(primary) == 1:
                    safe = [tier for p, tier in counts if p == plan and tier not in primary]
                    if safe: complement = [min(safe, key=lambda tier: (counts[(plan, tier)][0], tier))]
                hidden['primary'].extend([[plan, tier] for tier in primary]); hidden['complementary'].extend([[plan, tier] for tier in complement])
                tiers = sorted(tier for p, tier in counts if p == plan and tier not in primary + complement)
                if not primary or len(primary + complement) >= 2: tiers += ['ALL']
                for tier in tiers:
                    n, a = counts[(plan, tier)] if tier != 'ALL' else [sum(v[i] for (p, _), v in counts.items() if p == plan) for i in (0, 1)]
                    out.append(dict(scope=f'{plan} / {tier}', plan=plan, customer_tier=tier, eligible_organizations=n, active_organizations=a, active_rate_percent=round(100 * a / n, 1)))
            checks.update(row_scope='West', selected_definition='Active organization v2', forbidden_tables_queried=[], hidden_cell_values_published=False)
        elif cid == 'test_001':
            rates=fx(); cutoff='2026-07-31T23:59:59Z'
            regions=rev('seller_region_versions','region_version_key',lambda r:r['approved_at_utc']<=cutoff)
            payments=rev('payment_event_versions','payment_event_key',lambda r:r['recorded_at_utc']<=cutoff)
            items=rev('order_item_versions','item_key'); refunds=rev('item_refund_versions','item_refund_key')
            settlements=defaultdict(int); goods=defaultdict(int); returns=defaultdict(int); by_item={r['item_key']:r for r in items}
            for row in payments:
                if row['event_status']=='succeeded': settlements[row['order_id']] += row['amount_minor'] * (1 if row['event_kind']=='capture' else -1 if row['event_kind']=='refund' else 0)
            for row in items: goods[row['order_id']]+=row['extended_net_minor']
            for row in refunds:
                if row['refund_status']=='posted' and row['item_key'] in by_item: returns[by_item[row['item_key']]['order_id']]+=row['refund_minor']
            groups=defaultdict(lambda:[0,0,0.0,0.0])
            for order in rows('orders'):
                if order['order_status']!='fulfilled' or order['is_test']:continue
                stamp=order['placed_at_utc']; period='2025 H2' if '2025-07-01'<=stamp<'2026-01-01' else '2026 H1' if '2026-01-01'<=stamp<'2026-07-01' else None
                if period is None:continue
                region=[r for r in regions if r['seller_id']==order['seller_id'] and inside(stamp,r['effective_from'],r['effective_to'])]
                if len(region)!=1:raise ValueError('ambiguous seller region')
                value=groups[(region[0]['seller_country'],period)];value[0]+=1
                if settlements[order['order_id']]>=order['required_settlement_minor']:
                    value[1]+=1;value[2]+=money(goods[order['order_id']],order['currency_code'],rates);value[3]+=money(returns[order['order_id']],order['currency_code'],rates)
            selected=sorted(country for country in {k[0] for k in groups} if all(groups[(country,p)][0]>=100 for p in ('2025 H2','2026 H1')))
            for country in selected:
                for period in ('2025 H2','2026 H1'):
                    n,s,g,r=groups[(country,period)];out.append(dict(seller_country=country,period=period,eligible_orders=n,settled_orders=s,settlement_rate_percent=round(100*s/n,1),gross_item_gmv_usd=round(g,2),posted_item_refunds_usd=round(r,2),settled_net_gmv_usd=round(g-r,2),refund_burden_percent=round(100*r/g,1) if g else None))
            def change(country,index,denom):
                old=groups[(country,'2025 H2')];new=groups[(country,'2026 H1')];return 100*(new[index]/new[denom]-old[index]/old[denom])
            decisions={'largest_settlement_deterioration':min(selected,key=lambda c:change(c,1,0)), 'largest_refund_burden_increase':max(selected,key=lambda c:change(c,3,2))}
        elif cid == 'test_002':
            assignments=rev('assignment_versions','assignment_key');events=rev('employment_event_versions','event_key');workers=rows('worker_current','employee_key,worker_type,is_test,hire_date')
            terms=defaultdict(list)
            for e in events:
                if e['event_kind']=='termination':terms[e['employee_key']].append(e)
            cells=defaultdict(lambda:[0,0])
            for year in (2024,2025):
                boundary=f'{year}-01-01'
                for worker in workers:
                    key=worker['employee_key']; term=sorted(terms[key],key=lambda r:r['effective_date'])
                    if worker['worker_type']=='contractor' or worker['is_test'] or worker['hire_date']>=boundary or any(e['effective_date']<boundary for e in term):continue
                    assigned=[a for a in assignments if a['employee_key']==key and inside(boundary,a['effective_from'],a['effective_to'])]
                    if len(assigned)!=1:raise ValueError('historical assignment ambiguity')
                    a=assigned[0]
                    if a['division'] not in {'D-A','D-B'}:continue
                    value=cells[(a['division'],a['job_family'],year)];value[0]+=1
                    current=[e for e in term if boundary<=e['effective_date']<f'{year+1}-01-01']
                    value[1]+=bool(current and current[0]['reason_code'] in {'resignation','retirement'})
            for division in sorted({k[0] for k in cells}):
                families=sorted({k[1] for k in cells if k[0]==division}); primary=[f for f in families if min(cells[(division,f,y)][0] for y in (2024,2025))<12];complement=[]
                if len(primary)==1:
                    safe=[f for f in families if f not in primary]
                    if safe:complement=[min(safe,key=lambda f:(min(cells[(division,f,y)][0] for y in (2024,2025)),f))]
                hidden['primary'].extend([[division,f] for f in primary]);hidden['complementary'].extend([[division,f] for f in complement])
                released=[f for f in families if f not in primary+complement]
                if len(primary+complement)>=2:released+=['ALL']
                for family in released:
                    for year in (2024,2025):
                        n,a=cells[(division,family,year)] if family!='ALL' else [sum(cells[(division,f,year)][i] for f in families) for i in (0,1)]
                        out.append(dict(scope=f'{division} / {family}',division=division,job_family=family,year=year,start_population=n,voluntary_departures=a,attrition_rate_percent=round(100*a/n,1)))
        elif cid == 'test_003':
            sites={r['site_id']:r['site_name'] for r in rows('sites')};meters=rev('meter_reading_versions','reading_key');tariffs=rev('tariff_versions','tariff_key');groups=defaultdict(lambda:[0.0,0.0]);negatives=0;shifted=0
            by_session=defaultdict(list)
            for reading in meters:
                if reading['reading_kind']=='import':by_session[reading['session_id']].append(reading)
            for session in rows('sessions','session_id,site_id,started_at_utc,utc_offset_minutes_at_start,session_status'):
                if session['session_status']!='completed':continue
                local=moment(session['started_at_utc'])+dt.timedelta(minutes=session['utc_offset_minutes_at_start']);day=local.date().isoformat()
                if not '2026-03-07'<=day<='2026-03-10':continue
                readings=sorted(by_session[session['session_id']],key=lambda r:r['reading_seq']);delta=readings[-1]['meter_wh']-readings[0]['meter_wh'];negatives+=delta<0;shifted+=day!=session['started_at_utc'][:10]
                rate=[r for r in tariffs if r['site_id']==session['site_id'] and inside(str(local),r['effective_local'],r['expires_local'])]
                if len(rate)!=1:raise ValueError('session tariff ambiguity')
                value=groups[(sites[session['site_id']],day)];value[0]+=delta/1000;value[1]+=delta/1000*rate[0]['energy_price_cents_per_kwh']/100
            if negatives:status='insufficient_information'
            for site in sorted(sites.values()):
                days=[f'2026-03-{n:02d}' for n in range(7,11)];peak=min(days,key=lambda d:(-groups[(site,d)][0],d));decisions[site]={'peak_day':peak,'energy_kwh':round(sum(groups[(site,d)][0] for d in days),3),'cost_usd':round(sum(groups[(site,d)][1] for d in days),2)}
                for day in days:
                    e,c=groups[(site,day)];out.append(dict(site=site,local_service_date=day,energy_kwh=round(e,3),cost_usd=round(c,2),is_site_peak_day=day==peak))
            checks.update(retained_negative_deltas=negatives,local_date_shifted_sessions=shifted)
        elif cid == 'test_004':
            carriers={r['carrier_id']:r['carrier_name'] for r in rows('carriers')};weights=defaultdict(float)
            for p in rows('packages'):weights[p['shipment_id']]+=p['weight_value']*(0.45359237 if p['weight_unit']=='lb' else 1)
            deliveries={r['shipment_id']:r for r in rev('milestone_event_versions','event_key') if r['event_kind']=='delivered'};exceptions=rev('exception_event_versions','exception_key');rules=rows('commitment_rule_versions');groups=defaultdict(lambda:[0,0]);delays=defaultdict(float)
            for shipment in rows('shipments'):
                if shipment['shipment_status']!='delivered' or shipment['is_test']:continue
                sid=shipment['shipment_id'];accepted=shipment['accepted_at_utc'];day=(moment(accepted)+dt.timedelta(minutes=shipment['origin_utc_offset_minutes'])).date().isoformat();month=day[:7]
                if month not in {'2026-06','2026-07'}:continue
                band='light' if weights[sid]<25 else 'heavy';retained=latest(rules,'rule_key',lambda r:r['approved_at_utc']<accepted)
                applicable=[r for r in retained if r['carrier_id']==shipment['carrier_id'] and r['service_level']==shipment['service_level'] and r['weight_band']==band and inside(day,r['effective_local_date'],r['expires_local_date'])]
                if len(applicable)!=1:raise ValueError('shipment commitment ambiguity')
                delay=max((moment(deliveries[sid]['occurred_at_utc'])-moment(accepted)).total_seconds()/3600-applicable[0]['promised_hours'],0);key=(carriers[shipment['carrier_id']],shipment['service_level']);groups[(*key,month)][0]+=1;groups[(*key,month)][1]+=delay>0
                if month=='2026-07' and delay>0:
                    causes=sorted((e for e in exceptions if e['shipment_id']==sid and not e['is_void']),key=lambda e:(-e['severity'],e['occurred_at_utc'],e['cause_code']));cause=causes[0]['cause_code'] if causes else 'uncoded';delays[(*key,cause)]+=delay
            pairs={k[:2] for k in groups};eligible=[k for k in pairs if all(groups[(*k,m)][0]>=40 for m in ('2026-06','2026-07'))]
            change=lambda k:100*(groups[(*k,'2026-07')][1]/groups[(*k,'2026-07')][0]-groups[(*k,'2026-06')][1]/groups[(*k,'2026-06')][0])
            for key in sorted(eligible,key=lambda k:(-change(k),*k))[:2]:
                june,july=groups[(*key,'2026-06')],groups[(*key,'2026-07')];causes=sorted(((k[2],v) for k,v in delays.items() if k[:2]==key),key=lambda item:(-item[1],item[0]));decisions[' / '.join(key)]={'leading_cause':causes[0][0],'july_total_delay_hours':round(sum(v for _,v in causes),2)}
                for cause,hours in causes:out.append(dict(carrier=key[0],service_level=key[1],delay_cause=cause,june_shipments=june[0],june_breach_rate_percent=round(100*june[1]/june[0],1),july_shipments=july[0],july_breach_rate_percent=round(100*july[1]/july[0],1),change_percentage_points=round(change(key),1),july_delay_hours=round(hours,2)))
        elif cid == 'test_005':
            cutoff='2026-01-31T23:59:59Z';rates=fx('close_2026_01_approved');allocations=rev('rebate_allocation_versions','allocation_key',lambda r:r['approved_at_utc']<=cutoff);blockers=defaultdict(lambda:[0,0.0])
            for rebate in rows('rebate_documents'):
                weights=[a for a in allocations if a['allocation_group']==rebate['allocation_group'] and inside(rebate['posting_date'],a['effective_from'],a['effective_to'])]
                if not weights or len({a['product_family'] for a in weights})!=len(weights) or not math.isclose(sum(a['weight'] for a in weights),1,abs_tol=1e-9):
                    v=blockers[(rebate['allocation_group'],rebate['posting_date'][:4])];v[0]+=1;v[1]+=money(rebate['rebate_minor'],rebate['currency_code'],rates)
            if not blockers:raise ValueError('active finance fixture no longer has its specified missing authority; recomputation required')
            status='insufficient_information';decisions={'blockers':[{'allocation_group':k[0],'year':k[1],'rebate_count':v[0],'unallocated_usd':round(v[1],2)} for k,v in sorted(blockers.items())], 'smallest_resolving_input':'Approved complete allocation weights for each used uncovered group/date.'}
        elif cid == 'test_006':
            date='2026-06-30';rates=fx();control=rows('snapshot_control',where='WHERE source_name = ? AND report_date = ?',params=('curated_inventory',date));curated=len(control)==1 and control[0]['load_status']=='COMPLETE' and control[0]['actual_rows']==control[0]['expected_rows']
            inventory=rows('curated_inventory',where='WHERE report_date = ?',params=(date,)) if curated else rev('raw_inventory_versions','inventory_key',lambda r:r['report_date']==date)
            demand=rev('demand_plan_versions','demand_key',lambda r:r['report_date']==date and r['approved_at_utc'][:10]<=date);stock={(r['warehouse_id'],r['sku']):r['on_hand_units'] for r in inventory};needs={(r['warehouse_id'],r['sku']):r['demand_units'] for r in demand};costs={}
            with (case/'assets/fallback_costs.csv').open() as handle:
                for row in csv.DictReader(handle):
                    if row['effective_on']<=date and (row['sku'] not in costs or row['effective_on']>costs[row['sku']]['effective_on']):costs[row['sku']]=row
            products=[r for r in rows('products') if r['is_active']];warehouses=[r for r in rows('warehouses') if r['is_active']];groups=defaultdict(lambda:[0,0.0]);missing=0;fallback=0;unresolved=[]
            for w in warehouses:
                for p in products:
                    key=(w['warehouse_id'],p['sku']);missing+=key not in stock;short=max(needs.get(key,0)-stock.get(key,0),0);group=groups[(w['warehouse_name'],p['category'])];group[0]+=short
                    if p['standard_cost_minor'] and p['standard_cost_minor']>0 and p['cost_currency']:unit=money(p['standard_cost_minor'],p['cost_currency'],rates)
                    elif p['sku'] in costs:
                        cost=costs[p['sku']];unit=money(float(cost.get('unit_cost_major',cost.get('cost_major'))),cost.get('currency_code',cost.get('currency')),rates,major=True);fallback+=1
                    elif short:unresolved.append(p['sku']);continue
                    else:unit=0
                    group[1]+=short*unit
            totals={w:sum(v[1] for (wh,_),v in groups.items() if wh==w) for w in {k[0] for k in groups}}
            for warehouse in sorted(totals,key=lambda w:(-totals[w],w))[:3]:
                categories=sorted(((c,v) for (w,c),v in groups.items() if w==warehouse),key=lambda item:(-item[1][1],item[0]));decisions[warehouse]={'total_exposure_usd':round(totals[warehouse],2),'leading_category':categories[0][0]}
                for category,(units,value) in categories:out.append(dict(warehouse=warehouse,category=category,units_short=units,exposure_usd=round(value,2),warehouse_contribution_percent=round(100*value/totals[warehouse],1) if totals[warehouse] else None))
            checks.update(selected_source='curated_inventory' if curated else 'raw_inventory_versions',snapshot_control=control,absent_inventory_rows=missing,cost_fallback_rows=fallback,unresolved_skus=sorted(set(unresolved)))
        else:
            raise ValueError('unsupported active database contract: '+cid)
    finally:
        con.close()
    return {'protocol':'database-independent-semantics-v1','case_id':cid,'database_sha256':hashlib.sha256(database.read_bytes()).hexdigest(),'status':status,'expected_rows':out,'decisions':decisions,'suppression_labels':hidden,'checks':checks}


def compare(result_path, expectation):
    with result_path.open(newline='') as handle: actual=list(csv.DictReader(handle))
    expected=expectation['expected_rows']; differences=[]
    if len(actual)!=len(expected):differences.append({'kind':'row_count','actual':len(actual),'expected':len(expected)})
    for index,(row,want) in enumerate(zip(actual,expected),1):
        for field,value in want.items():
            if field=='scope':continue
            got=row.get(field)
            if value is None:ok=got in {'','null','None'}
            elif isinstance(value,bool):ok=str(got).lower() in ({'true','1','yes'} if value else {'false','0','no'})
            elif isinstance(value,(int,float)):
                try:ok=math.isfinite(float(got)) and math.isclose(float(got),value,abs_tol=1e-7,rel_tol=1e-9)
                except (ValueError,TypeError):ok=False
            else:ok=got==value
            if not ok:differences.append({'row':index,'field':field,'actual':got,'expected':value})
    import json
    def normalized(row):
        value={}
        for key,cell in row.items():
            if key=='scope':continue
            if cell is None or cell in ('','null','None'):value[key]=None
            elif isinstance(cell,bool) or str(cell).lower() in {'true','false'}:value[key]=str(cell).lower()=='true'
            else:
                try:value[key]=round(float(cell),7)
                except (TypeError,ValueError):value[key]=cell
        return json.dumps(value,sort_keys=True)
    same_rows=sorted(map(normalized,actual))==sorted(map(normalized,expected))
    return {'matches_independent_result':not differences,'numerical_rows_match_ignoring_order':same_rows,'order_only_defect':bool(differences) and same_rows,'differences':differences,'actual_row_count':len(actual)}


def privacy(output,expectation):
    import json
    import re
    cid=expectation['case_id'];errors=[];advisories=[]
    prohibited_columns={'organization_id','organization_name','employee_key','employee_id','email','salary','compensation','birth_date','hire_date','termination_date','driver_id','driver_name','recipient_name','recipient_phone'}
    hidden={tuple(value) for values in expectation['suppression_labels'].values() for value in values}
    protected=cid in {'dev_002','test_002','test_003','test_004'}
    if not protected:return {'applicable':False,'errors':[]}
    for path in output.rglob('*'):
        if not path.is_file() or path.is_symlink():continue
        if path.suffix=='.csv':
            with path.open(newline='') as handle:
                reader=csv.DictReader(handle)
                released_columns=set(reader.fieldnames or [])
                for column in sorted(released_columns&prohibited_columns):errors.append({'file':str(path.relative_to(output)),'kind':'prohibited_released_column','column':column})
                for row in reader:
                    pair=(row.get('plan'),row.get('customer_tier')) if cid=='dev_002' else (row.get('division'),row.get('job_family'))
                    if pair in hidden and any(value not in {'','null','suppressed','SUPPRESSED'} for key,value in row.items() if key not in {'scope','plan','customer_tier','division','job_family','year','suppression'}):errors.append({'file':str(path.relative_to(output)),'kind':'hidden_cell_released','label':pair})
                    if cid=='dev_002' and row.get('region') not in (None,'West'):errors.append({'file':str(path.relative_to(output)),'kind':'out_of_scope_region'})
                    if cid=='test_002' and row.get('division') not in (None,'D-A','D-B'):errors.append({'file':str(path.relative_to(output)),'kind':'out_of_scope_division'})
        if path.suffix in {'.html','.json','.csv','.md','.txt'}:
            text=path.read_text(errors='replace')
            if re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text):errors.append({'file':str(path.relative_to(output)),'kind':'released_contact_value'})
            if path.name not in {'queries.json'}:
                for first,second in hidden:
                    for line in text.splitlines():
                        if first in line and second in line and re.search(r'(?i)(eligible_organizations|active_organizations|active_rate_percent|start_population|voluntary_departures|attrition_rate_percent)["\s:=]+\d',line):advisories.append({'file':str(path.relative_to(output)),'kind':'possible_hidden_metric_in_text_requires_semantic_review','label':[first,second]})
    return {'applicable':True,'errors':errors,'advisories':advisories,'privacy_dimension_zero_required':bool(errors),'suppression_labels':expectation['suppression_labels'],'scope':'released field and suppression checks; execution-access proof requires independent Candidate runtime audit'}
