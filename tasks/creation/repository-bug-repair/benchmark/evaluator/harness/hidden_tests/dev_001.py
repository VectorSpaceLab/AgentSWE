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
