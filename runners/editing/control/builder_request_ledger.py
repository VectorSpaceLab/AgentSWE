"""Evaluator-private, immutable upper-Builder request identities and receipts."""
from __future__ import annotations
import hashlib
import fcntl
import json
import os
from pathlib import Path
from responses_stream import strict_json


def sha(data): return hashlib.sha256(data).hexdigest()


def write_new(path, value):
    with path.open('x') as f:
        json.dump(value,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())


class RequestLedger:
    def __init__(self, root: Path):
        self.root=root
        if root.is_symlink(): raise ValueError('request state cannot be symlinked')
        root.mkdir(parents=True,exist_ok=True)
        self.lock = (root/'.process.lock').open('a')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise

    def close(self):
        self.lock.close()

    def claim(self, body, route, provider_url):
        identity={'body':body,'route':route,'provider_url':provider_url,
                  'protocol':'agentswe-builder-single-upstream/v1'}
        digest=sha(json.dumps(identity,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())
        path=self.root/digest
        try:path.mkdir()
        except FileExistsError:
            if path.is_symlink():raise ValueError('request state was replaced by a symlink')
            receipt=path/'completed.json'
            if not receipt.is_file(): return path, None, 'existing_request_pending_or_unknown'
            value=strict_json(receipt.read_bytes())
            payload=(path/'response.bin').read_bytes()
            if value.get('identity')!=digest or value.get('payload_sha256')!=sha(payload) or value.get('status')!=200 or value.get('content_type') not in ('application/json','text/event-stream') and not str(value.get('content_type','')).startswith(('application/json;','text/event-stream;')):
                return path,None,'completed_response_integrity_failure'
            return path,{'payload':payload,'status':value['status'],'content_type':value['content_type']},None
        # The persisted directory itself is an intent even if interrupted here.
        write_new(path/'intent.json',{'identity':digest,'body_sha256':sha(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()),
                                     'route':route,'protocol':identity['protocol']})
        return path,None,None

    def sent(self, path):
        write_new(path/'upstream_started.json',{'actual_upstream_requests':1,'identity':path.name})

    def complete(self,path,*,payload,status,content_type,usage):
        with (path/'response.bin').open('xb') as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        write_new(path/'completed.json',{'identity':path.name,'status':status,
                                        'content_type':content_type,'payload_sha256':sha(payload),
                                        'actual_upstream_requests':1,'completed_responses':1,'usage':usage})

    def fail(self,path,*,error,status,sent):
        write_new(path/'failure.json',{'identity':path.name,'error':error,'status':status,
                                      'actual_upstream_requests':int(sent),'outcome':'unknown' if sent else 'not_sent',
                                      'usage':None if sent else {'input_tokens':0,'output_tokens':0,'total_tokens':0},
                                      'automatic_retry_allowed':False})

    def recover(self):
        """Rebuild usage from durable receipts; crashed intents remain frozen."""
        result = dict(calls=0,completed=0,failed=0,sent=0,unknown_usage=0,
                      input_tokens=0,output_tokens=0,total_tokens=0)
        for path in self.root.iterdir():
            if path.name.startswith('.'):
                continue
            if path.is_symlink() or not path.is_dir() or len(path.name)!=64 or any(c not in '0123456789abcdef' for c in path.name):
                raise ValueError('unexpected Builder request ledger entry')
            result['calls'] += 1
            sent = (path/'upstream_started.json').exists()
            if sent:
                record = strict_json((path/'upstream_started.json').read_bytes())
                if record != {'actual_upstream_requests':1,'identity':path.name}:
                    raise ValueError('invalid upstream start evidence')
            result['sent'] += int(sent)
            completed = path/'completed.json'
            failure = path/'failure.json'
            if completed.exists():
                record = strict_json(completed.read_bytes())
                if not sent or failure.exists() or record.get('identity')!=path.name or record.get('payload_sha256')!=sha((path/'response.bin').read_bytes()) or record.get('status')!=200:
                    raise ValueError('inconsistent Builder completion receipt')
                result['completed'] += 1
                usage = record.get('usage')
                if isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens','total_tokens')):
                    for key in ('input_tokens','output_tokens','total_tokens'):
                        result[key] += usage[key]
                else:
                    result['unknown_usage'] += 1
            else:
                result['failed'] += 1
                result['unknown_usage'] += int(sent)
                if failure.exists():
                    record = strict_json(failure.read_bytes())
                    if record.get('identity')!=path.name or record.get('actual_upstream_requests')!=int(sent):
                        raise ValueError('inconsistent Builder failure receipt')
                elif not (path/'recovered_interruption.json').exists():
                    write_new(path/'recovered_interruption.json',{'identity':path.name,
                        'outcome':'unknown' if sent else 'not_sent','actual_upstream_requests':int(sent),
                        'automatic_retry_allowed':False,'recovery':'intent_or_response_interrupted_before_terminal_receipt'})
        return result
