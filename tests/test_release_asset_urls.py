"""Release-asset URLs are percent-encoded: OSWorld task files with spaces in their names are fetched from the release
asset store like any other file (stdlib unittest; a local HTTP server stands in for the store)."""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import optimization_assets as oa  # noqa: E402


class Cfg:
    def __init__(self, home: Path, url: str):
        self.home, self.url = home, url

    def get(self, key, default=None):
        return {"AGENTSWE_RELEASE_ASSETS_URL": self.url}.get(key, default)


class ReleaseAssetUrls(unittest.TestCase):
    def test_release_paths_with_spaces_exist_in_osworld(self):
        files = json.loads((ROOT / "tasks/optimization/osworld/task.json").read_text())["runner_config"]["assets"]["files"]
        self.assertTrue(any(" " in f.get("release_path", "") for f in files))

    def test_fetch_encodes_the_release_path(self):
        saved = {k: os.environ.pop(k, None) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")}
        with tempfile.TemporaryDirectory() as tmp:
            store, home = Path(tmp) / "store", Path(tmp) / "home"
            rel = "osworld/fixture-downloads/abc_04 Purchasing info 2021 Jan.docx"
            data = b"docx bytes"
            (store / rel).parent.mkdir(parents=True)
            (store / rel).write_bytes(data)
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(store))
            handler.log_message = lambda *a, **k: None
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                spec = {"root": "assets/osworld", "files": [{"path": "fixture-downloads/abc_04 Purchasing info 2021 Jan.docx",
                                                            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                                                            "urls": [], "release_path": rel}]}
                url = f"http://127.0.0.1:{server.server_address[1]}"
                placed = oa.fetch_assets(Cfg(home, url), spec)
                self.assertEqual(len(placed), 1)
                self.assertEqual((home / "assets/osworld/fixture-downloads/abc_04 Purchasing info 2021 Jan.docx").read_bytes(), data)
            finally:
                server.shutdown()
                server.server_close()
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
