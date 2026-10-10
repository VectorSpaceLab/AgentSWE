"""The Result-judge broker's cleanup waits for an --rm container the daemon is still removing (stdlib unittest,
docker mocked).

JudgeBroker containers run with --rm. After `docker stop`, Docker 29 removes them asynchronously; a `docker rm -f` in
that window answers "removal of container ... is already in progress", and the old check=True turned that into
cleanup_error_type CalledProcessError and absent_after_cleanup False (cleanup_unverified), although the container
was gone a few seconds later. Docker 24 had already removed it, so `rm -f` succeeded."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runners" / "editing" / "control"))

import judge_broker_runtime as runtime  # noqa: E402

PRESENT = {"State": {"Running": False, "Status": "removing"}}


class JudgeCleanupRace(unittest.TestCase):
    def broker(self, tmp):
        obj = runtime.JudgeBroker(name="probe", credential=Path(tmp) / "fake.env", image="mock", port=1234,
                                  cidfile=Path(tmp) / "owned.cid")
        obj.directory.mkdir()
        obj.container_id = "a" * 64
        return obj

    def close(self, obj, inspections, rm_result):
        def command(argv, **kwargs):
            if argv[:2] == ["docker", "stop"]:
                return subprocess.CompletedProcess(argv, 0, obj.container_id + "\n", "")
            if argv[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(argv, *rm_result)
            raise AssertionError(argv)
        with mock.patch.object(obj, "_inspect", side_effect=inspections), \
                mock.patch.object(runtime, "stats", return_value={"runtime": {}}), \
                mock.patch.object(runtime.subprocess, "run", side_effect=command), \
                mock.patch.object(runtime.time, "sleep"):
            return obj.close()

    def test_removal_already_in_progress_is_awaited(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self.broker(tmp)
            busy = (1, "", "Error response from daemon: removal of container %s is already in progress" % ("a" * 64))
            result = self.close(obj, [PRESENT, PRESENT, PRESENT, PRESENT, None, None], busy)
        self.assertTrue(result["absent_after_cleanup"])
        self.assertTrue(result["removal_in_progress_at_rm"])
        self.assertNotIn("cleanup_error_type", result)

    def test_docker_24_path_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self.broker(tmp)
            result = self.close(obj, [PRESENT, PRESENT, None, None], (0, "", ""))
        self.assertTrue(result["absent_after_cleanup"])
        self.assertNotIn("removal_in_progress_at_rm", result)

    def test_another_rm_refusal_is_still_a_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self.broker(tmp)
            result = self.close(obj, [PRESENT, PRESENT], (1, "", "Error response from daemon: permission denied"))
        self.assertFalse(result["absent_after_cleanup"])
        self.assertEqual(result["cleanup_error_type"], "CalledProcessError")

    def test_a_container_that_never_goes_away_is_not_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            obj = self.broker(tmp)
            busy = (1, "", "removal of container x is already in progress")
            with mock.patch.object(runtime, "REMOVAL_WAIT_SECONDS", 0):
                result = self.close(obj, [PRESENT] * 6, busy)
        self.assertFalse(result["absent_after_cleanup"])
        self.assertNotIn("cleanup_error_type", result)


if __name__ == "__main__":
    unittest.main()
