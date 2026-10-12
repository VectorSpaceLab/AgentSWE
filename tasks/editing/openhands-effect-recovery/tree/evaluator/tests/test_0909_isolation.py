from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lower_agent"))
import openhands_lower_agent as lower
from isolated_product import product_server_source
from isolated_runtime import request


class IsolationTests(unittest.TestCase):
    def test_candidate_server_contains_no_model_call_or_private_world(self):
        source = product_server_source(lower, "test_001", "user-visible task", "a" * 32)
        self.assertNotIn("createCaseWorld", source)
        self.assertNotIn("async function askModel", source)
        self.assertNotIn("AGENT_SERVER_ENDPOINT", source)
        self.assertNotIn("private_world_config", source)
        self.assertIn("/bridge/world.sock", source)
        self.assertIn("await dispatch(adapter", source)

    def test_public_world_socket_denies_oracle_and_keeps_real_state(self):
        with tempfile.TemporaryDirectory(prefix="oh-test-") as temp:
            root = Path(temp)
            config = {"case_id": "test_002", "nonce": "a" * 32, "fixture": {"workspaceChunkResponseLoss": True},
                "public_socket": str(root / "public.sock"), "private_socket": str(root / "private.sock"), "product_socket": str(root / "product.sock")}
            path = root / "config.json"; path.write_text(json.dumps(config))
            process = subprocess.Popen(["node", "--experimental-strip-types", str(ROOT / "lower_agent/world_service.ts"), str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if Path(config["public_socket"]).exists(): break
                    if process.poll() is not None: self.fail(process.stderr.read().decode())
                    time.sleep(.02)
                public = Path(config["public_socket"])
                private = Path(config["private_socket"])
                self.assertIn("error", request(public, "/snapshot", {}, 2))
                self.assertIn("error", request(public, "/before", {"action": "cancel"}, 2))
                initial = request(public, "/init", {}, 2)["result"]
                self.assertNotIn("comparisons", initial)
                scope = initial["scope"]
                local = request(public, "/io", {"method": "local.capture", "input": scope}, 2)["result"]
                entry = next(item for item in local["entries"] if item["path"] != "README.md")
                chunk = request(public, "/io", {"method": "local.readChunk", "input": {**scope, "digest": entry["contentDigest"]}}, 2)["result"]
                lost = request(public, "/io", {"method": "transport.writeChunk", "input": {**scope, "digest": entry["contentDigest"], "bytes": chunk}}, 2)
                self.assertIn("response lost", lost["error"])
                missing = request(public, "/io", {"method": "transport.missingChunks", "input": {**scope, "digests": [entry["contentDigest"]]}}, 2)["result"]
                self.assertEqual(missing, [])
                snapshot = request(private, "/snapshot", {}, 2)["result"]
                self.assertEqual(snapshot["observed"]["operations"][-1]["operation"], "fault.chunk_response_lost")
            finally:
                process.terminate(); process.wait(timeout=5)
                process.stdout.close(); process.stderr.close()


if __name__ == "__main__": unittest.main()
