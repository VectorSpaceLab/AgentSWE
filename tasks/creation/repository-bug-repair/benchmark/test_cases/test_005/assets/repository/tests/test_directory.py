import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from profiledirectory import DirectoryService, Profile
from profiledirectory.repository import ProfileRepository


class DirectoryTests(unittest.TestCase):
    def make_service(self):
        return DirectoryService(ProfileRepository([Profile("t", "u", "a@test", "A")]))

    def test_cold_read(self):
        self.assertEqual(self.make_service().get_user("t", "u")["email"], "a@test")

    def test_update_returns_new_value(self):
        service = self.make_service()
        self.assertEqual(service.update_user("t", "u", email="b@test")["email"], "b@test")

    def test_result_is_defensive(self):
        service = self.make_service()
        result = service.get_user("t", "u")
        result["email"] = "changed"
        self.assertEqual(service.get_user("t", "u")["email"], "a@test")


if __name__ == "__main__":
    unittest.main()
