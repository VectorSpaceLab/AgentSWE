"""Probe through the broker against the fake provider (offline)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FAKE_KEY, FakeProvider, Script, responses_completed, tmpdir  # noqa: E402
from agentswe_broker import __main__ as cli  # noqa: E402
from agentswe_broker.probe import png_1x1, run_probe  # noqa: E402
from agentswe_broker.server import Options  # noqa: E402


class Probe(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()

    def tearDown(self):
        self.provider.close()

    def options(self, **kw):
        base = dict(role="judge", upstream_wire="responses", provider_url=self.provider.base + "/responses",
                    model="deepseek-flash", effort="max", keepalive_seconds=0.2)
        base.update(kw)
        return Options(**base)

    def test_all_capabilities_present(self):
        self.provider.add(Script(200, json.dumps(responses_completed("OK")).encode()),
                          Script(200, json.dumps(responses_completed('{"ok": true}')).encode()),
                          Script(200, json.dumps(responses_completed("yes")).encode()))
        report = run_probe(self.options(), FAKE_KEY, image=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(report["checks"]), {"basic", "json_schema", "image_input"})
        bodies = [r["body"] for r in self.provider.requests]
        self.assertTrue(all(b["model"] == "deepseek-flash" and b["reasoning"] == {"effort": "max"} for b in bodies))
        self.assertEqual(bodies[1]["text"]["format"]["type"], "json_schema")
        self.assertTrue(bodies[2]["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn(FAKE_KEY, json.dumps(report))

    def test_model_echo_and_schema_failures_are_reported(self):
        self.provider.add(Script(200, json.dumps(responses_completed("OK", model="other-name")).encode()),
                          Script(200, json.dumps(responses_completed("not json")).encode()))
        report = run_probe(self.options(role="builder"), FAKE_KEY)
        self.assertEqual(report["role"], "builder")
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["basic"]["model_echo"])
        self.assertEqual(report["checks"]["basic"]["reported_model"], "other-name")
        self.assertFalse(report["checks"]["json_schema"]["ok"])

    def test_provider_rejection(self):
        self.provider.add(Script(400, b'{"error": {"message": "unknown effort"}}'),
                          Script(400, b'{"error": {"message": "unknown effort"}}'))
        report = run_probe(self.options(), FAKE_KEY)
        self.assertEqual(report["checks"]["basic"]["http_status"], 400)
        self.assertFalse(report["ok"])

    def test_png_is_valid(self):
        import zlib
        data = png_1x1()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(zlib.decompress(data[41:41 + int.from_bytes(data[33:37], "big")]), b"\x00\xff\xff\xff\xff")

    def test_cli_probe(self):
        work = tmpdir()
        envfile = work / ".env"
        envfile.write_text("AGENTSWE_JUDGE_BASE_URL=%s\nAGENTSWE_JUDGE_API_KEY=%s\n" % (self.provider.base, FAKE_KEY))
        self.provider.add(Script(200, json.dumps(responses_completed("OK")).encode()),
                          Script(200, json.dumps(responses_completed('{"ok": true}')).encode()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["probe", "--role", "judge", "--env-file", str(envfile), "--keepalive-seconds", "0.2"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn(FAKE_KEY, out.getvalue())


if __name__ == "__main__":
    unittest.main()
