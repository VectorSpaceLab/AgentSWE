"""Provider-free checks for the controlled lower proxy container contract."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lower_broker_runtime as runtime


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class LowerProxyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.credential = self.root / "credential.env"
        self.credential.write_text("OPENAI_API_KEY=not-used\n")
        self.cidfile = self.root / "lower.cid"
        self.cid = "a" * 64

    def inspect(self):
        evidence = self.root / "lower-lower-transport"
        return json.dumps([{
            "Id": self.cid,
            "Image": "sha256:unit",
            "Config": {"Labels": {}, "Cmd": []},
            "Mounts": [{"Source": str(evidence), "Destination": "/evidence"}],
        }]).encode()

    def test_runtime_injects_exact_controlled_proxy_and_requires_it_in_health(self):
        health = {"model": "deepseek-flash", "reasoning_effort": "high",
                  "protocol": "agentswe-lower-single-upstream/v1",
                  "provider_proxy_url": "http://127.0.0.1:7890"}
        with patch.object(runtime.subprocess, "run") as run:
            run.return_value.stdout = self.cid + "\n"
            with patch.object(runtime.subprocess, "check_output", return_value=self.inspect()):
                with patch.object(runtime.urllib.request, "urlopen",
                                  return_value=Response(json.dumps(health).encode())):
                    runtime.start_lower_broker(name="unit", credential=self.credential,
                        image="unit-image", port=18081, cidfile=self.cidfile)
        command = run.call_args.args[0]
        self.assertIn("AGENTSWE_EVALUATOR_PROXY_URL=http://127.0.0.1:7890", command)
        index = command.index("AGENTSWE_EVALUATOR_PROXY_URL=http://127.0.0.1:7890")
        self.assertEqual(command[index - 1], "-e")

    def test_health_without_exact_proxy_never_becomes_ready(self):
        health = {"model": "deepseek-flash", "reasoning_effort": "high",
                  "protocol": "agentswe-lower-single-upstream/v1",
                  "provider_proxy_url": None}
        with patch.object(runtime.subprocess, "run") as run:
            run.return_value.stdout = self.cid + "\n"
            with patch.object(runtime.subprocess, "check_output", return_value=self.inspect()):
                with patch.object(runtime.urllib.request, "urlopen",
                                  side_effect=lambda *_args, **_kwargs:
                                      Response(json.dumps(health).encode())):
                    with patch.object(runtime.time, "sleep"):
                        with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                            runtime.start_lower_broker(name="unit", credential=self.credential,
                                image="unit-image", port=18081, cidfile=self.cidfile)


if __name__ == "__main__":
    unittest.main()
