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
