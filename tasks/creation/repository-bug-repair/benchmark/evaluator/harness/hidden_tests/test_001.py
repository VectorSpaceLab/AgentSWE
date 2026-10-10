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
