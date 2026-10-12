"""Task-local adapters for the actual AI Scientist broker ledgers."""
def lower(raw, request_id):
    if raw.get('schema_version')!='agentswe-broker-stats-v2': raise ValueError('AI lower ledger schema mismatch')
    rows=raw.get('attempts',[]); runtime=raw.get('runtime',{})
    row=next((x for x in rows if x.get('request_id')==request_id),None)
    if row is None or row.get('state')!='terminal' or row.get('usage_unknown'): raise ValueError('AI lower request unknown')
    usage=row.get('usage') or {}; total=usage.get('total_tokens')
    if not isinstance(total,int) or total<0 or row.get('upstream_attempts')!=1: raise ValueError('AI lower usage malformed')
    if runtime.get('failures',0) or runtime.get('usage_unknown_calls',0): raise ValueError('AI lower aggregate failed')
    return {'request_id':request_id,'state':'success','upstream_attempts':1,'known_tokens':total,'usage_known':True}

def judge(raw, request_id):
    if raw.get('schema_version')!='agentswe-judge-broker-stats/v1': raise ValueError('AI Result ledger schema mismatch')
    rows=raw.get('attempts',[]); runtime=raw.get('runtime',{})
    if len(rows)!=1 or runtime.get('calls')!=runtime.get('successful_calls')!=1 or runtime.get('failures')!=0: raise ValueError('Result ledger incomplete')
    row=rows[0]; usage=row.get('usage') or {}; total=usage.get('total_tokens')
    if row.get('request_id')!=request_id or row.get('state')!='terminal' or row.get('usage_unknown') or not isinstance(total,int): raise ValueError('Result attempt incomplete')
    return {'request_id':request_id,'state':'success','upstream_attempts':1,'known_tokens':total,'usage_known':True}

def code(raw, request_id):
    if raw.get('schema_version')!='agentswe-judge-http-attempts/v1' or raw.get('logical_requests')!=1: raise ValueError('AI Code ledger schema mismatch')
    rows=raw.get('attempts',[])
    if len(rows)!=1: raise ValueError('Code attempts incomplete')
    row=rows[0]; usage=row.get('usage') or {}; total=usage.get('total_tokens')
    if row.get('response_id')!=request_id or row.get('state')!='terminal' or row.get('http_status')!=200 or row.get('response_status')!='completed' or row.get('usage_known') is not True or not isinstance(total,int): raise ValueError('Code attempt incomplete')
    return {'request_id':request_id,'state':'success','upstream_attempts':1,'known_tokens':total,'usage_known':True}

def make(): return {'public_lower':lower,'hidden_lower':lower,'result_judge':judge,'code_judge':code}
