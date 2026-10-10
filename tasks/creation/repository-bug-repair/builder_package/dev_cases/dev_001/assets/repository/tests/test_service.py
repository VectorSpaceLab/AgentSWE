import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from parcelroute import QuoteService


class QuoteServiceTests(unittest.TestCase):
    def test_quote_shape_and_price(self):
        result = QuoteService().quote("acct", 1500, "EU")
        self.assertEqual(result["amount_cents"], 280)
        self.assertEqual(result["destination"], "EU")

    def test_identical_request_reuses_cache(self):
        service = QuoteService()
        first = service.quote("acct", 500, "LOCAL")
        second = service.quote("acct", 500, "LOCAL")
        self.assertEqual(first, second)
        self.assertEqual(len(service.cache), 1)

    def test_validation(self):
        with self.assertRaisesRegex(ValueError, "destination"):
            QuoteService().quote("acct", 1000, "")


if __name__ == "__main__":
    unittest.main()
