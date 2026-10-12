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
