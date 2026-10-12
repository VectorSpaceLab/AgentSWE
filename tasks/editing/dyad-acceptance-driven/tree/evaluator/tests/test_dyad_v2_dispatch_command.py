"""Provider-free tests for the task-local Dyad v2 dispatch constructor."""
from __future__ import annotations
import unittest
from pathlib import Path
from evaluator import v2_dispatch


class DyadV2DispatchCommandTests(unittest.TestCase):
    def command(self):
        return v2_dispatch.build_command(
            python=Path("/usr/bin/python3"),
            formal_one_stop=Path("/stage/harbor/formal_one_stop.py"),
            run_dir=Path("/run/dyad-v2"),
            hidden_cases_dir=Path("/evaluator-issued/dyad-v2"),
            credential_file=Path("/credential"),
            harbor=Path("/harbor"),
        )

    def test_command_is_explicit_v2_shape(self):
        command = self.command()
        result = v2_dispatch.validate_command(command)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["profile"], v2_dispatch.PROFILE)
        self.assertEqual(result["public_cases"], ["dev_001"])
        self.assertEqual(result["required_valid_rounds"], 2)
        self.assertEqual(result["hidden_cases"], ["test_001"])
        self.assertEqual(result["max_dev_rounds"], 2)
        self.assertEqual(result["n_concurrent"], 1)
        self.assertIn("--pilot", command)
        self.assertNotIn("--run-formal", command)

    def test_command_rejects_legacy_formal_shape(self):
        command = self.command() + ["--run-formal"]
        result = v2_dispatch.validate_command(command)
        self.assertFalse(result["valid"])
        self.assertIn("v2 readiness command cannot use --run-formal", result["errors"])

    def test_command_rejects_wrong_round_limit(self):
        command = self.command()
        index = command.index("--max-dev-rounds") + 1
        command[index] = "10"
        result = v2_dispatch.validate_command(command)
        self.assertFalse(result["valid"])
        self.assertIn("--max-dev-rounds must be 2", result["errors"])


if __name__ == "__main__":
    unittest.main()
