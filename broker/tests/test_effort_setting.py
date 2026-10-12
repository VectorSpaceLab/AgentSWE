"""Effort settings: unset spellings, explicit-none, and what the pinned request carries (no network)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentswe_broker import config  # noqa: E402
from agentswe_broker.server import Options, pin_chat_body, pin_responses_body  # noqa: E402


def options(effort):
    return Options(role="runtime", upstream_wire="responses", provider_url="http://127.0.0.1:9/v1/responses",
                   model="deepseek-flash", effort=config.normalize_effort(effort))


class EffortSetting(unittest.TestCase):
    def test_unset_spellings_leave_the_effort_out(self):
        for value in (None, "", "none", "None", "off", "unset"):
            self.assertIsNone(config.normalize_effort(value), value)
            body, receipt = pin_responses_body({"input": "x", "reasoning": {"effort": "high"}}, options(value))
            self.assertNotIn("reasoning", body)
            self.assertIsNone(receipt["reasoning_effort_sent"])

    def test_explicit_none_sends_the_literal_none(self):
        self.assertEqual(config.normalize_effort("explicit-none"), "none")
        body, receipt = pin_responses_body({"input": "x", "reasoning": {"effort": "explicit-none"}},
                                           options("explicit-none"))
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertEqual(receipt["reasoning_effort_sent"], "none")
        self.assertEqual(pin_chat_body({"messages": []}, options("explicit-none"))["reasoning_effort"], "none")

    def test_other_values_pass_as_given(self):
        for value in ("low", "medium", "high", "xhigh", "max"):
            body, _ = pin_responses_body({"input": "x"}, options(value))
            self.assertEqual(body["reasoning"], {"effort": value})

    def test_role_resolution_reads_the_same_way(self):
        env = {"AGENTSWE_DEFAULT_BASE_URL": "https://api.deepseek.com/v1", "AGENTSWE_DEFAULT_API_KEY": "k"}
        self.assertEqual(config.resolve("runtime", {**env, "AGENTSWE_RUNTIME_EFFORT": "explicit-none"}).effort, "none")
        self.assertIsNone(config.resolve("runtime", {**env, "AGENTSWE_RUNTIME_EFFORT": "none"}).effort)


if __name__ == "__main__":
    unittest.main()
