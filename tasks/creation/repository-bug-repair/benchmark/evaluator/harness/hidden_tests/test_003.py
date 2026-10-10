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
