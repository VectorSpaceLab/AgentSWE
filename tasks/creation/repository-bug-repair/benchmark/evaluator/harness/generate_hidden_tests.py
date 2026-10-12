#!/usr/bin/env python3
"""Generate evaluator-owned tests. Never distribute this file to builders."""

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]
TESTS = {
"dev_001.py": r'''
import unittest
from parcelroute import QuoteService
from parcelroute.cache import QuoteCache
from parcelroute.policy import PricingPolicy

class Tests(unittest.TestCase):
    def test_destination_service_currency_and_policy_are_identity(self):
        service = QuoteService()
        first = service.quote("acct", 1500, "EU", "economy", currency="USD")
        second = service.quote("acct", 1500, "APAC", "priority", currency="EUR")
        self.assertEqual((second["destination"], second["service_level"], second["currency"]), ("APAC", "priority", "EUR"))
        self.assertNotEqual(first["amount_cents"], second["amount_cents"])
        service.policy = PricingPolicy("new", {"EU": 999, "DEFAULT": 999}, {"economy": 100})
        changed = service.quote("acct", 1500, "EU", "economy", currency="USD")
        self.assertEqual(changed["policy_revision"], "new")
        self.assertNotEqual(first["amount_cents"], changed["amount_cents"])
    def test_normalized_alias_reuses_entry_and_lru_remains_bounded(self):
        service = QuoteService(cache=QuoteCache(2))
        self.assertEqual(service.quote(" acct ", 1000, " eu "), service.quote("acct", 1000, "EU"))
        self.assertEqual(len(service.cache), 1)
        service.quote("acct", 1000, "APAC")
        service.quote("acct", 1000, "LOCAL")
        self.assertEqual(len(service.cache), 2)
    def test_result_dictionary_is_defensive(self):
        service = QuoteService()
        result = service.quote("a", 1000, "EU")
        result["destination"] = "changed"
        self.assertEqual(service.quote("a", 1000, "EU")["destination"], "EU")
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"dev_002.py": r'''
import tempfile, unittest
from pathlib import Path
from sessionarchive.codec import encode_record
from sessionarchive.migrate import migrate_record
from sessionarchive.store import SessionArchive

class Tests(unittest.TestCase):
    def test_versions_decimal_extensions_and_v2_idempotence(self):
        for version in (None, 1, "1"):
            raw={"session_id":"s","user_id":"u","expires":"2.5005","metadata":{"r":1},"x":[3]}
            if version is not None: raw["version"]=version
            migrated=migrate_record(raw)
            self.assertEqual(migrated["expires_at_ms"],2501); self.assertEqual(migrated["x"],[3])
            self.assertEqual(migrate_record(migrated),migrated)
        v2={"version":2,"session_id":"v2","user_id":"u","expires_at_ms":7,"extra":{"k":[1]}}
        self.assertEqual(migrate_record(v2),v2)
    def test_invalid_expiry_and_versions_fail(self):
        for value in (True,-1,"-0.1","NaN","Infinity","bad"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                migrate_record({"version":1,"session_id":"s","user_id":"u","expires":value})
        with self.assertRaises(ValueError): migrate_record({"version":"2","session_id":"s","user_id":"u","expires_at_ms":1})
    def test_mixed_file_retry_preserves_original_rollback_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"s.jsonl"
            rows=[{"version":"1","session_id":"a","user_id":"u","expires":"1.25"},{"version":2,"session_id":"b","user_id":"u","expires_at_ms":7,"extra":True}]
            original=b"".join(encode_record(row) for row in rows); path.write_bytes(original)
            path.with_name(path.name+".tmp").write_bytes(b"incomplete")
            archive=SessionArchive(path); archive.migrate_file(); first=path.read_bytes(); archive.migrate_file()
            self.assertEqual(path.read_bytes(),first); self.assertEqual([row["expires_at_ms"] for row in archive.load_all()],[1250,7])
            backups=list(path.parent.glob(path.name+".bak*")); self.assertTrue(backups)
            self.assertIn(original,[backup.read_bytes() for backup in backups])
            self.assertFalse(path.with_name(path.name+".tmp").exists())
    def test_complete_temporary_state_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"s.jsonl"; path.write_bytes(encode_record({"version":1,"session_id":"a","user_id":"u","expires":1}))
            path.with_name(path.name+".tmp").write_bytes(encode_record({"version":2,"session_id":"a","user_id":"u","expires_at_ms":1000}))
            SessionArchive(path).migrate_file()
            self.assertEqual(SessionArchive(path).load_all()[0]["expires_at_ms"],1000)
            self.assertFalse(path.with_name(path.name+".tmp").exists())
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"test_001.py": r'''
import threading, unittest
from dispatchqueue import DispatchQueue
from dispatchqueue.consistency import audit_consistency

class Tests(unittest.TestCase):
    def test_duplicate_linearizable_ordered_and_replayable(self):
        for _ in range(4):
            queue=DispatchQueue(); barrier=threading.Barrier(24); results=[]
            def worker(index): barrier.wait(); results.append(queue.record_once("same",f"w{index}",{"n":{"v":index}}))
            threads=[threading.Thread(target=worker,args=(index,)) for index in range(24)]
            [thread.start() for thread in threads]; [thread.join(3) for thread in threads]
            self.assertTrue(all(not thread.is_alive() for thread in threads)); self.assertEqual(results.count(True),1)
            self.assertEqual(audit_consistency(queue),{"row_count":1,"audit_count":1,"duplicate_rows":0,"duplicate_audit":0,"same_order":True})
            rebuilt=DispatchQueue.from_journal(queue.journal); self.assertEqual(rebuilt.snapshot(),queue.snapshot())
    def test_distinct_ids_sequences_and_audit_order(self):
        queue=DispatchQueue(); threads=[threading.Thread(target=queue.record_once,args=(f"d{i}","w",{})) for i in range(80)]
        [thread.start() for thread in threads]; [thread.join(3) for thread in threads]
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        rows=queue.snapshot(); audit=queue.audit_snapshot()
        self.assertEqual({row["delivery_id"] for row in rows},{f"d{i}" for i in range(80)})
        self.assertEqual([row["sequence"] for row in rows],list(range(1,81)))
        self.assertEqual([row["delivery_id"] for row in rows],[row["delivery_id"] for row in audit])
    def test_failed_journal_releases_reservation_and_sequence(self):
        queue=DispatchQueue(); original=queue.journal.append
        queue.journal.append=lambda _row: (_ for _ in ()).throw(OSError("disk"))
        with self.assertRaises(OSError): queue.record_once("x","w",{})
        self.assertEqual(queue.snapshot(),[]); self.assertEqual(queue.audit_snapshot(),[])
        queue.journal.append=original
        self.assertTrue(queue.record_once("x","w",{})); self.assertEqual(queue.snapshot()[0]["sequence"],1)
    def test_nested_snapshot_and_replay_payloads_are_defensive(self):
        queue=DispatchQueue(); payload={"nested":{"items":[1]}}; queue.record_once("x","w",payload)
        payload["nested"]["items"].append(2)
        first=queue.snapshot(); first[0]["payload"]["nested"]["items"].append(3)
        self.assertEqual(queue.snapshot()[0]["payload"],{"nested":{"items":[1]}})
        replay=queue.audit_snapshot(); replay[0]["payload"]["nested"]["items"].append(4)
        self.assertEqual(queue.audit_snapshot()[0]["payload"],{"nested":{"items":[1]}})
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"test_002.py": r'''
import tempfile, threading, unittest
from pathlib import Path
from tenantconfig import ConfigRepository, create_v1_database, create_v3_database
from tenantconfig.db import connect
from tenantconfig.migrate import migrate, rollback

class Tests(unittest.TestCase):
    def test_decimal_preservation_idempotence_compatibility_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":"t","timeout":"2.3455","retries":4,"payload":{"label":"blue","x":[1]}}])
            migrate(path); self.assertEqual(ConfigRepository(path).load("t"),{"tenant_id":"t","timeout_ms":2346,"retries":4,"label":"blue","extension":{"x":[1]},"revision":1})
            self.assertEqual(migrate(path),0); rollback(path)
            self.assertEqual(ConfigRepository(path).load("t"),{"tenant_id":"t","timeout_ms":2346,"retries":4,"label":"blue","extension":{"x":[1]},"revision":0})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"v3.db"; create_v3_database(path); connection=connect(path)
            connection.execute("INSERT INTO tenant_settings VALUES (?,?,?,?,?,?)",("n",9,3,"v3",'{"z":2}',7)); connection.close()
            self.assertEqual(ConfigRepository(path).load("n")["revision"],7)
    def test_interrupted_table_version_combinations_recover(self):
        for version in (1,3):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":"t","timeout":"1.5"}])
                connection=connect(path); connection.execute("ALTER TABLE tenant_settings RENAME TO tenant_settings_v1"); connection.execute(f"PRAGMA user_version={version}"); connection.close()
                migrate(path); self.assertEqual(ConfigRepository(path).load("t")["timeout_ms"],1500)
    def test_invalid_row_rolls_back_without_partial_schema(self):
        for value in ("NaN","Infinity","-1","bad"):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":"ok","timeout":"1"},{"tenant_id":"bad","timeout":value}])
                with self.assertRaises(ValueError): migrate(path)
                connection=connect(path); tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0],1); self.assertIn("tenant_settings",tables); self.assertNotIn("migration_state",tables); connection.close()
                self.assertEqual(ConfigRepository(path).load("ok")["timeout_ms"],1000)
    def test_concurrent_migration_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":str(i),"timeout":"0.0015"} for i in range(20)])
            barrier=threading.Barrier(4); errors=[]
            def run():
                try: barrier.wait(); migrate(path)
                except Exception as exc: errors.append(exc)
            threads=[threading.Thread(target=run) for _ in range(4)]; [thread.start() for thread in threads]; [thread.join(8) for thread in threads]
            self.assertTrue(all(not thread.is_alive() for thread in threads)); self.assertEqual(errors,[])
            connection=connect(path); self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0],"ok"); self.assertEqual(connection.execute("SELECT COUNT(*) FROM tenant_settings").fetchone()[0],20); connection.close()
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"test_003.py": r'''
import time, tracemalloc, unittest
from eventmerge import merge_events

def event(timestamp,event_id,payload=None): return {"timestamp_ms":timestamp,"event_id":event_id,"payload":{"v":event_id} if payload is None else payload}
class Tests(unittest.TestCase):
    def test_exact_ids_unicode_stable_ties_and_no_shared_state(self):
        sources=[[event(1,"A"),event(2,"é")],[event(1,"a"),event(3,"é")]]
        self.assertEqual([item["event_id"] for item in merge_events(sources)],["A","a","é"])
        self.assertEqual([item["event_id"] for item in merge_events([[event(0,"A")]])],["A"])
    def test_generators_once_and_performance(self):
        counts=[0]*8
        def source(source_index):
            for index in range(10000): counts[source_index]+=1; yield event(index*8+source_index,f"{source_index}:{index}")
        tracemalloc.start(); started=time.perf_counter(); result=merge_events(source(index) for index in range(8)); elapsed=time.perf_counter()-started
        _current,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
        self.assertEqual(len(result),80000); self.assertEqual(counts,[10000]*8)
        self.assertLess(elapsed,2.0,f"elapsed={elapsed}"); self.assertLess(peak,120*1024*1024,f"peak={peak}")
    def test_empty_validation_and_unordered(self):
        self.assertEqual(merge_events([]),[])
        with self.assertRaises(ValueError): merge_events([[event(2,"x"),event(1,"y")]])
        with self.assertRaises(ValueError): merge_events([[{"timestamp_ms":True,"event_id":"x"}]])
        with self.assertRaises(ValueError): merge_events([[event(1,"")]])
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"test_004.py": r'''
import hashlib, unittest
from wirebatch import FrameParser, ProtocolError
from wirebatch.encode import encode_frame

class Tests(unittest.TestCase):
    def parse_chunks(self,wire,chunks,**kwargs):
        parser=FrameParser(**kwargs); output=[]
        for chunk in chunks: output.extend(parser.feed(chunk))
        parser.finish(); return output
    def assert_code(self,wire,code,**kwargs):
        with self.assertRaises(ProtocolError) as caught: FrameParser(**kwargs).feed(wire)
        self.assertEqual(caught.exception.code,code)
    def test_every_octet_boundary_multiple_binary_and_utf8(self):
        wire=encode_frame("héllo".encode(),content_type=" text/ü ",request_id="r",checksum=True)+encode_frame(b"\x00\xff")
        output=self.parse_chunks(wire,[wire[index:index+1] for index in range(len(wire))])
        self.assertEqual([frame["payload"] for frame in output],["héllo".encode(),b"\x00\xff"])
        self.assertEqual(output[0]["headers"]["content-type"],"text/ü")
    def test_split_after_headers_payload_and_terminator(self):
        wire=encode_frame(b"abcdef"); cut=wire.index(b"\r\n\r\n")+4
        chunks=[wire[:cut],wire[cut:cut+2],wire[cut+2:-1],wire[-1:]]
        self.assertEqual(self.parse_chunks(wire,chunks)[0]["payload"],b"abcdef")
    def test_length_header_and_encoding_codes(self):
        cases=[
            (b"BAD 0\r\nContent-Type: x\r\n\r\n\r\n","length_line"),
            (b"LEN +1\r\nContent-Type: x\r\n\r\na\r\n","length_value"),
            (b"LEN 0\r\nNoColon\r\n\r\n\r\n","header_syntax"),
            (b"LEN 0\r\nBad Name: x\r\nContent-Type: x\r\n\r\n\r\n","header_name"),
            (b"LEN 0\r\nX: \xff\r\nContent-Type: x\r\n\r\n\r\n","header_encoding"),
            (b"LEN 0\r\nContent-Type: x\r\ncontent-type: y\r\n\r\n\r\n","duplicate_header"),
            (b"LEN 0\r\n\r\n\r\n","missing_header"),
            (b"LEN 0\r\nContent-Type: \t \r\n\r\n\r\n","missing_header"),
        ]
        for wire,code in cases:
            with self.subTest(code=code): self.assert_code(wire,code)
    def test_checksum_terminator_and_no_partial_frame(self):
        digest=hashlib.sha256(b"abc").hexdigest()
        self.assert_code(f"LEN 3\r\nContent-Type: x\r\nChecksum-SHA256: {digest[:-1]}0\r\n\r\nabc\r\n".encode(),"checksum")
        self.assert_code(b"LEN 1\r\nContent-Type: x\r\n\r\naXX","frame_terminator")
        parser=FrameParser(); self.assertEqual(parser.feed(b"LEN 3\r\nContent-Type: x\r\n\r\na"),[])
        with self.assertRaises(ProtocolError) as caught: parser.finish()
        self.assertEqual(caught.exception.code,"truncated_frame")
    def test_payload_header_buffer_limits_and_configuration(self):
        self.assert_code(b"LEN 4\r\nContent-Type: x\r\n\r\n", "payload_limit", max_payload_bytes=3)
        self.assert_code(b"LEN 0\r\nX-Long: 1234567890\r\nContent-Type: x\r\n\r\n\r\n", "header_limit", max_header_bytes=20)
        with self.assertRaises(ProtocolError) as caught: FrameParser(max_payload_bytes=1,max_header_bytes=1).feed(b"x"*100)
        self.assertEqual(caught.exception.code,"buffer_limit")
        for args in ((0,10),(10,0),(True,10)):
            with self.assertRaises(ValueError): FrameParser(*args)
    def test_finish_at_boundary_and_case_insensitive_names(self):
        parser=FrameParser(); output=parser.feed(b"LEN 0\r\ncOnTeNt-TyPe: x\r\n\r\n\r\n"); parser.finish()
        self.assertEqual(output[0]["headers"],{"content-type":"x"})
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"test_005.py": r'''
import unittest
from profiledirectory import DirectoryService, Profile
from profiledirectory.batch import update_many
from profiledirectory.repository import ProfileRepository

class CountingRepository(ProfileRepository):
    def __init__(self,profiles=None): super().__init__(profiles); self.get_count=0
    def get(self,tenant_id,user_id): self.get_count+=1; return super().get(tenant_id,user_id)

class Tests(unittest.TestCase):
    def test_warm_positive_exact_tenant_invalidation_and_cache_hit(self):
        repository=CountingRepository([Profile("a","u","a@x","A"),Profile("b","u","b@x","B")]); service=DirectoryService(repository)
        service.get_user("a","u"); service.get_user("b","u"); self.assertEqual(repository.get_count,2)
        service.get_user("b","u"); self.assertEqual(repository.get_count,2)
        service.update_user("a","u",email="a2@x")
        self.assertEqual(service.get_user("a","u")["email"],"a2@x"); self.assertEqual(service.get_user("b","u")["email"],"b@x")
        self.assertEqual(repository.get_count,3)
    def test_negative_cache_invalidated_by_create(self):
        service=DirectoryService(); self.assertIsNone(service.get_user("t","u")); service.create_user("t","u","x@x","X")
        self.assertEqual(service.get_user("t","u")["email"],"x@x")
    def test_nested_results_are_defensive(self):
        profile=Profile("t","u","a@x","A",attributes={"prefs":{"tags":["one"]}}); service=DirectoryService(ProfileRepository([profile]))
        result=service.get_user("t","u"); result["attributes"]["prefs"]["tags"].append("two")
        self.assertEqual(service.get_user("t","u")["attributes"],{"prefs":{"tags":["one"]}})
    def test_batch_rollback_revisions_and_commit_time_invalidation(self):
        repository=ProfileRepository([Profile("t","a","a@x","A"),Profile("t","b","b@x","B")]); service=DirectoryService(repository)
        service.get_user("t","a"); service.get_user("t","b"); before=repository.snapshot()
        events=[]; service.events.subscribe(lambda event: events.append((event,repository.get("t","a"),repository.get("t","b"))))
        with self.assertRaises((KeyError,ValueError)): update_many(service,"t",[{"user_id":"a","email":"new@x"},{"user_id":"missing","email":"m@x"}])
        self.assertEqual(repository.snapshot(),before); self.assertEqual(events,[]); self.assertEqual(service.get_user("t","a")["email"],"a@x")
        result=update_many(service,"t",[{"user_id":"a","email":"a2@x"},{"user_id":"b","display_name":"B2"}])
        self.assertEqual([item["revision"] for item in result],[2,2]); self.assertEqual(len(events),2)
        self.assertTrue(all(a.email=="a2@x" and b.display_name=="B2" for _event,a,b in events))
        self.assertEqual((service.get_user("t","a")["email"],service.get_user("t","b")["display_name"]),("a2@x","B2"))
if __name__ == "__main__": unittest.main(verbosity=2)
''',
"test_006.py": r'''
import json, struct, tempfile, unittest
from pathlib import Path
from segmentstore import SegmentStore, StoreCorruption
from segmentstore.format import MAX_RECORD_BYTES, encode_record
from segmentstore.manifest import write_manifest

class Tests(unittest.TestCase):
    def active_segment(self,root):
        manifest=json.loads((root/"MANIFEST.json").read_text()); return root/manifest["segments"][-1]
    def test_torn_final_tail_ignored_without_rewrite_then_append_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=SegmentStore(root); store.append({"seq":1}); path=self.active_segment(root)
            original=path.read_bytes(); torn=encode_record({"seq":2})[:7]; path.write_bytes(original+torn)
            self.assertEqual(store.load(),[{"seq":1}]); self.assertEqual(path.read_bytes(),original+torn)
            store.append({"seq":3}); self.assertEqual(store.load(),[{"seq":1},{"seq":3}]); self.assertNotIn(torn,path.read_bytes()[len(original):])
    def test_complete_corruption_impossible_length_and_malformed_json_raise_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=SegmentStore(root); store.append({"seq":1}); path=self.active_segment(root)
            data=bytearray(path.read_bytes()); data[-1]^=1; path.write_bytes(data)
            with self.assertRaises(StoreCorruption) as caught: store.load()
            self.assertIn(str(path),str(caught.exception)); self.assertEqual(caught.exception.offset,0)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=SegmentStore(root); store.initialize(); path=self.active_segment(root); path.write_bytes(struct.pack(">I",MAX_RECORD_BYTES+1))
            with self.assertRaises(StoreCorruption) as caught: store.load()
            self.assertEqual(caught.exception.offset,0)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=SegmentStore(root); store.initialize(); path=self.active_segment(root)
            payload=b"not-json"; import zlib; path.write_bytes(struct.pack(">I",len(payload))+payload+struct.pack(">I",zlib.crc32(payload)&0xffffffff))
            with self.assertRaises(StoreCorruption): store.load()
    def test_torn_or_missing_nonfinal_segment_is_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); first=root/"segment-000000.bin"; second=root/"segment-000001.bin"; root.mkdir(exist_ok=True)
            first.write_bytes(encode_record({"seq":1})[:5]); second.write_bytes(encode_record({"seq":2})); write_manifest(root/"MANIFEST.json",{"generation":1,"segments":[first.name,second.name]})
            with self.assertRaises(StoreCorruption): SegmentStore(root).load()
            first.unlink()
            with self.assertRaises(StoreCorruption): SegmentStore(root).load()
    def test_pending_newer_complete_commits_incomplete_rolls_back_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=SegmentStore(root); store.append({"seq":1}); new=root/"segment-000001.bin"; new.write_bytes(encode_record({"seq":9}))
            write_manifest(root/"MANIFEST.next",{"generation":1,"segments":[new.name]})
            self.assertEqual(store.recover(),"committed"); self.assertEqual(store.load(),[{"seq":9}]); self.assertEqual(store.recover(),"clean")
            bad=root/"segment-000002.bin"; bad.write_bytes(encode_record({"seq":10})[:5]); write_manifest(root/"MANIFEST.next",{"generation":2,"segments":[bad.name]})
            self.assertEqual(store.recover(),"rolled_back"); self.assertEqual(store.load(),[{"seq":9}]); self.assertFalse((root/"MANIFEST.next").exists())
    def test_stale_pending_and_only_proven_temporaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=SegmentStore(root); store.append({"seq":1}); store.compact(); active=(root/"MANIFEST.json").read_bytes()
            old=root/"segment-old.bin"; old.write_bytes(encode_record({"seq":0})); write_manifest(root/"MANIFEST.next",{"generation":0,"segments":[old.name]})
            stale=root/"segment-999999.bin.tmp"; stale.write_bytes(b"partial"); keep=root/"notes.tmp"; keep.write_bytes(b"keep")
            self.assertEqual(store.recover(),"rolled_back"); self.assertEqual((root/"MANIFEST.json").read_bytes(),active); self.assertFalse(stale.exists()); self.assertTrue(keep.exists())
    def test_manifest_segment_paths_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); root.mkdir(exist_ok=True); outside=root.parent/"outside.bin"; outside.write_bytes(encode_record({"seq":1}))
            write_manifest(root/"MANIFEST.json",{"generation":0,"segments":["../outside.bin"]})
            with self.assertRaises(StoreCorruption): SegmentStore(root).load()
if __name__ == "__main__": unittest.main(verbosity=2)
''',
}


def main() -> None:
    target = ROOT / "evaluator/harness/hidden_tests"
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.py"):
        old.unlink()
    for name, source in TESTS.items():
        (target / name).write_text(dedent(source).lstrip(), encoding="utf-8")
    print(f"generated {len(TESTS)} evaluator-owned test modules")


if __name__ == "__main__":
    main()
