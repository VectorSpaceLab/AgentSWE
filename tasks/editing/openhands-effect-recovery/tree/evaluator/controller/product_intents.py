"""Evaluator-private durable reservations; no automatic recovery or replay."""
import json
import os
from pathlib import Path
import time


def durable_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.pending')
    with temporary.open('w') as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


class ProductIntents:
    def __init__(self, root, *, name='product_intents.json', context=None):
        self.path = Path(root) / name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {'schema_version':'agentswe-openhands-product-intents/v1',
            'context':context or {}, 'initialized_at_ns':time.time_ns(), 'entries':{}}
        try:
            with self.path.open('x') as output:
                output.write(json.dumps(self.data, indent=2) + '\n')
                output.flush(); os.fsync(output.fileno())
        except FileExistsError as exc:
            raise RuntimeError('Existing product intents require reconciliation; automatic restart/replay is forbidden') from exc
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)

    @property
    def entries(self): return self.data['entries']

    def persist(self): durable_json(self.path, self.data)

    def reserve(self, identity, **metadata):
        if identity in self.entries:
            return self.entries[identity]
        row = {'state':'unknown', 'source_identity':identity, 'reserved_at_ns':time.time_ns(),
            'metadata':metadata, 'cases':{}}
        self.entries[identity] = row
        self.persist()
        return row

    def case_started(self, identity, case_id):
        row=self.entries[identity]
        if case_id in row['cases']:
            raise RuntimeError('Case already reserved; replay is forbidden')
        row['cases'][case_id]={'state':'unknown','reserved_at_ns':time.time_ns()}
        self.persist()

    def case_finished(self, identity, case_id, result):
        valid=result.get('result_evaluation',{}).get('contract_valid') is True and result.get('result_evaluation',{}).get('round_consumed') is True
        self.entries[identity]['cases'][case_id].update(state='completed' if valid else 'unknown',
            result=result, returned_at_ns=time.time_ns())
        self.persist()

    def finish(self, identity, state, result):
        row=self.entries[identity]
        if row['state']=='completed':
            return row['result']
        row.update(state=state, result=result, recorded_at_ns=time.time_ns())
        self.persist()
        return result
