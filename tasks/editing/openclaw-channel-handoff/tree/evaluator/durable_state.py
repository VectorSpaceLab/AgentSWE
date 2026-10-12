"""Independent, schema-neutral evidence from a stopped product's SQLite state.

Never import Candidate code or run its SQL/views/triggers. Preserve main/WAL/
journal bytes before opening a separate read-only SQL working copy. Column/table names and
values are Candidate-controlled data, not evaluator instructions or truth.
This collector supplies evidence to the semantic judge; it computes no score.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

FILES = ('openclaw.sqlite', 'openclaw.sqlite-wal', 'openclaw.sqlite-shm', 'openclaw.sqlite-journal')
MAX_ROWS = 4096
MAX_PROJECTION_BYTES = 2 * 1024 * 1024
ENUMS = frozenset(('pending claimed prepared accepted_unverified acked verified failed active running '
    'paused cancelled canceled completed stopped delivered handled ignored unknown unverified '
    'queued uploading staged ready complete incomplete direct group thread user_result internal_control '
    'full summary text/plain application/octet-stream webchat default').split())


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def expected_from_facts(facts, case_id):
    """Derive identities from the private dynamic input, never from DB answers."""
    from evaluator.case_service import PrivateFacts, attachment_bytes
    values = PrivateFacts(**facts)
    if values.case_id != case_id or values.bundle_version != 'openclaw-native-case-v2':
        raise ValueError('wrong or stale durable observation case')
    task = 'task-'+hashlib.sha256(values.task_nonce.encode()).hexdigest()[:24]
    principal = 'principal-'+hashlib.sha256((values.task_nonce+':principal').encode()).hexdigest()[:20]
    note = ('Update '+task+': the requested case result is ready.').encode()
    asset = attachment_bytes(values.task_nonce,values.attachment_size)
    if hashlib.sha256(asset).hexdigest() != values.attachment_sha256:
        raise ValueError('private attachment facts do not match current case')
    expected = {'task_id':task,'principal_id':principal,'source_peer':values.route_thread or values.route_peer,
        'result_body_sha256':hashlib.sha256(note).hexdigest(),'requested_result_body':note,
        'attachment_sha256':values.attachment_sha256}
    if asset:
        expected['requested_attachment_bytes'] = asset
    hidden = [values.task_nonce,values.route_peer,values.route_thread,values.callback_token]
    return expected, hidden


def bound_durable_evidence(record, case_id):
    """Do not accept a transplanted snapshot or an old unbound cache entry."""
    value=record.get('durable_state')
    if not isinstance(value,dict) or value.get('collection_valid') is not True:
        return False
    binding=value.get('case_binding',{})
    native=record.get('native_case',{})
    unit=record.get('case_resources',{}).get('unit')
    bundle=native.get('case_bundle_sha256') if isinstance(native,dict) else None
    return bool(binding.get('case_id') == case_id and isinstance(bundle,str) and len(bundle)==64
        and binding.get('case_bundle_sha256') == bundle
        and isinstance(unit,str) and unit and binding.get('owned_scope_unit') == unit
        and value.get('writers_stopped') is True and value.get('source_bytes_unchanged') is True)


class EvidenceUnavailable(RuntimeError):
    pass


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise EvidenceUnavailable('case_deadline_exhausted')


def open_directory(path):
    """Resolve no symlinks, including ancestor components of writable state."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise EvidenceUnavailable('state_directory_not_absolute')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_source(directory, name, deadline, destination=None):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvidenceUnavailable('state_entry_not_owned_regular_file')
        digest = hashlib.sha256()
        output = destination.open('xb') if destination else None
        try:
            while True:
                check_deadline(deadline)
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if output:
                    output.write(chunk)
            if output:
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o600)
        finally:
            if output:
                output.close()
        if fingerprint(before) != fingerprint(os.fstat(fd)):
            raise EvidenceUnavailable('state_changed_during_copy')
        return {'sha256': digest.hexdigest(), 'bytes': before.st_size,
                'identity': list(fingerprint(before))}
    finally:
        os.close(fd)


class Projection:
    """Keep counters/contract enums; replace all other text with run-local HMAC.

    Expected-value matches are computed from evaluator-owned dynamic facts,
    not copied from Candidate claims. Bodies and authority values never appear.
    The random HMAC key remains only in memory and is never passed to the judge.
    """
    def __init__(self, expected, private_values=()):
        self.key = secrets.token_bytes(32)
        self.expected = expected
        self.private_values = tuple(value for value in private_values if isinstance(value,str) and value)
        self.matches = {}

    def opaque(self, value):
        return hmac.new(self.key, value, hashlib.sha256).hexdigest()

    def text(self, value):
        encoded = value.encode('utf-8')
        result = {'kind': 'text', 'utf8_bytes': len(encoded), 'identity': self.opaque(encoded)}
        matches = sorted(key for key, expected in self.expected.items()
                         if isinstance(expected, str) and value == expected)
        if matches:
            result['matches_expected'] = matches
            for match in matches:
                self.matches[match] = self.matches.get(match, 0) + 1
        if value in ENUMS:
            result['contract_enum'] = value
        return result

    def value(self, value, depth=0):
        if depth > 24:
            raise EvidenceUnavailable('projection_nesting_exceeds_reader_capacity')
        if value is None or isinstance(value, (int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else {'kind':'nonfinite_number'}
        if isinstance(value, bytes):
            digest = hashlib.sha256(value).hexdigest()
            matches = sorted(key for key, expected in self.expected.items()
                             if isinstance(expected, bytes) and digest == hashlib.sha256(expected).hexdigest()
                             and value == expected)
            return {'kind':'blob', 'bytes':len(value), 'identity':self.opaque(value),
                    'matches_expected':matches}
        if isinstance(value, str):
            # JSON is stored data, never evaluated as program text. The JSON
            # representation is not required: normalized SQLite works too.
            if value[:1] in ('{', '['):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    pass
                else:
                    return {'kind':'stored_json', 'value':self.value(parsed,depth+1)}
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item,depth+1) for item in value]
        if isinstance(value, dict):
            # Keys are also untrusted and can contain secrets. Display only
            # short conventional field names, not arbitrary database text.
            return {self.name(str(key)):self.value(item,depth+1) for key,item in value.items()}
        raise EvidenceUnavailable('unsupported_sqlite_value')

    def name(self, value):
        safe = (value.isascii() and len(value) <= 80 and value.replace('_','').isalnum()
                and not value[:1].isdigit()
                and not any(isinstance(item,str) and item and item in value
                            for item in (*self.expected.values(), *self.private_values)))
        return value if safe else 'opaque_name_'+self.opaque(value.encode())


def read_snapshot(database, *, deadline, expected, private_values=()):
    projection = Projection(expected,private_values)
    # immutable=1 omitted committed WAL rows on the pinned Python SQLite.
    # Normal read-only mode reads WAL and may update its private shm file.
    # This path is a disposable reader copy, never original/raw evidence.
    connection = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=0)
    phase='security_configuration'
    try:
        connection.enable_load_extension(False)
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        if connection.execute('PRAGMA trusted_schema').fetchone() != (0,):
            raise EvidenceUnavailable('reader_cannot_disable_trusted_schema')
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)

        def authorize(action, first, second, database_name, trigger):
            if trigger is not None:
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and first in {'table_xinfo', 'quick_check'}:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        phase='quick_check'
        check = connection.execute('PRAGMA quick_check').fetchall()
        if check != [('ok',)]:
            return {'reader_complete':True, 'sqlite_integrity_ok':False,
                    'integrity_issue_count':len(check), 'tables':[],
                    'semantic_success_inferred':False}
        phase='schema_inventory'
        # LIKE is a SQL function and intentionally denied by the authorizer.
        # Do metadata filtering in trusted Python, not by enabling functions.
        schema = connection.execute("SELECT name, rootpage, sql, type FROM sqlite_schema ORDER BY name").fetchall()
        tables, row_count, output_bytes, skipped = [], 0, 0, []
        for name, page, sql, kind in schema:
            check_deadline(deadline)
            if name.startswith('sqlite_') or kind in {'index','trigger'}:
                continue
            if kind != 'table':
                skipped.append({'object':projection.name(name),'reason':'view_not_executed'})
                continue
            if not page or not isinstance(sql,str) or 'CREATE VIRTUAL TABLE' in sql.upper():
                skipped.append({'table':projection.name(name), 'reason':'virtual_table_not_executed'})
                continue
            quoted = '"'+name.replace('"','""')+'"'
            phase='stored_columns'
            columns = connection.execute('PRAGMA table_xinfo('+quoted+')').fetchall()
            stored = [column[1] for column in columns if column[6] == 0]
            omitted = [projection.name(column[1]) for column in columns if column[6] != 0]
            entry = {'table':projection.name(name), 'schema_sha256':hashlib.sha256(sql.encode()).hexdigest(),
                     'columns':[projection.name(column) for column in stored], 'rows':[],
                     'nonstored_columns_not_evaluated':omitted}
            if stored:
                phase='stored_rows'
                selection = ','.join('"'+column.replace('"','""')+'"' for column in stored)
                for row in connection.execute('SELECT '+selection+' FROM '+quoted):
                    check_deadline(deadline)
                    value = [projection.value(item) for item in row]
                    row_count += 1
                    output_bytes += len(canonical(value))
                    if row_count > MAX_ROWS or output_bytes > MAX_PROJECTION_BYTES:
                        raise EvidenceUnavailable('projection_exceeds_reader_capacity_no_semantic_judgment')
                    entry['rows'].append(value)
            tables.append(entry)
        return {'reader_complete':True,
                'all_schema_objects_observed':not skipped and all(not t['nonstored_columns_not_evaluated'] for t in tables),
                'sqlite_integrity_ok':True, 'tables':tables, 'row_count':row_count,
                'skipped_tables':skipped, 'expected_value_occurrences':projection.matches,
                'sqlite_version':sqlite3.sqlite_version, 'candidate_code_executed':False,
                'reader_mode':'mode=ro with WAL on a separate private working copy',
                'semantic_success_inferred':False,
                'interpretation':'Stored fields are independent of RPC claims, but stored claims alone do not prove causality or correct authorization.'}
    except sqlite3.Error as exc:
        raise EvidenceUnavailable('sqlite_reader:'+phase+':'+type(exc).__name__) from exc
    finally:
        connection.close()


def capture_durable_state(*, state, output, deadline, expected, writers_stopped, private_values=()):
    """Called after owned Gateway/client shutdown, within the same case budget."""
    started = time.monotonic()
    result = {'schema_version':'openclaw-independent-durable-evidence/v1',
              'collection_valid':False, 'writers_stopped':writers_stopped,
              'candidate_code_executed':False, 'semantic_score_computed':False,
              'scope':'fixed public state/openclaw.sqlite path; no prescribed handoff table names',
              'raw_snapshot_evaluator_only':True}
    directory = None
    try:
        if writers_stopped is not True:
            raise EvidenceUnavailable('product_writers_not_verified_stopped')
        check_deadline(deadline)
        output = Path(output)
        output.mkdir(parents=True,exist_ok=False,mode=0o700)
        raw = output/'raw'; raw.mkdir(mode=0o700)
        parent = open_directory(Path(state))
        try:
            try:
                directory = os.open('state',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            except FileNotFoundError:
                directory = None
        finally:
            os.close(parent)
        before = {name:read_source(directory,name,deadline,raw/name) if directory is not None else None for name in FILES}
        after = {name:read_source(directory,name,deadline) if directory is not None else None for name in FILES}
        if before != after:
            raise EvidenceUnavailable('state_changed_after_product_shutdown')
        result['snapshot_files'] = before
        result['source_bytes_unchanged'] = True
        result['database_present'] = before['openclaw.sqlite'] is not None
        if result['database_present']:
            reader = output/'reader';reader.mkdir(mode=0o700)
            raw_directory = open_directory(raw.absolute())
            try:
                for name in FILES:
                    read_source(raw_directory,name,deadline,reader/name)
            finally:
                os.close(raw_directory)
            result['observations'] = read_snapshot(reader/'openclaw.sqlite',deadline=deadline,
                                                  expected=expected,private_values=private_values)
            # Reader locks/shm never touch the retained raw copy or source.
            raw_directory = open_directory(raw.absolute())
            try:
                for name, original in before.items():
                    copied = read_source(raw_directory,name,deadline)
                    if (copied is None) != (original is None) or (original and
                        any(copied[key] != original[key] for key in ('sha256','bytes'))):
                        raise EvidenceUnavailable('snapshot_modified_by_reader')
            finally:
                os.close(raw_directory)
            if before != {name:read_source(directory,name,deadline) for name in FILES}:
                raise EvidenceUnavailable('source_changed_during_independent_read')
            result['collection_valid'] = result['observations']['reader_complete']
            if not result['collection_valid']:
                result['error'] = 'reader_cannot_completely_observe_this_schema'
        else:
            # A missing product DB is an observed fact, not a collector outage
            # and not an automatic whole-task zero.
            result['collection_valid'] = True
            result['observations'] = {'database_absent':True,'semantic_success_inferred':False}
    except (OSError, ValueError, sqlite3.Error, EvidenceUnavailable, RecursionError) as exc:
        result['error'] = str(exc) if isinstance(exc,EvidenceUnavailable) else type(exc).__name__
    finally:
        if directory is not None:
            os.close(directory)
    result['elapsed_seconds'] = time.monotonic()-started
    return result
