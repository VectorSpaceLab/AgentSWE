"""`agentswe --env-file <path>`: a named dotenv file that does not exist is an error (stdlib unittest; no network).

Before, `agentswe --env-file /wrong/path/.env config` exited 0 and printed `"env_file": null`: a mistyped path went on
without the keys or with the wrong configuration. The library call config.load(path) keeps treating a missing file
as an empty .env (tests and tools use it that way); only the command line, which names the file, refuses.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import cli, config  # noqa: E402

CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}


class EnvFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, CLEAN_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_a_missing_env_file_stops_the_command_but_not_the_library_call(self):
        missing = self.tmp / "no-such.env"
        with self.assertRaises(SystemExit) as ctx:
            self.main("--env-file", str(missing), "config")
        message = str(ctx.exception.code)
        self.assertIn(f"--env-file {missing}: no such file", message)
        self.assertIn("leave out --env-file", message)
        cfg = config.load(missing)  # the library call keeps treating a missing file as an empty .env
        self.assertIsNone(cfg.env_file)
        self.assertEqual(cfg.role("JUDGE").model, "deepseek-flash")

    def test_a_directory_is_refused_and_a_file_is_read(self):
        with self.assertRaises(SystemExit) as ctx:
            self.main("--env-file", str(self.tmp), "config")
        self.assertIn("not a regular file", str(ctx.exception.code))
        env = self.tmp / "agentswe.env"
        env.write_text(f"AGENTSWE_HOME={self.tmp / 'home'}\nAGENTSWE_JUDGE_MODEL=judge-from-file\n")
        code, out = self.main("--env-file", str(env), "config")
        self.assertEqual(code, 0)
        described = json.loads(out)
        self.assertEqual(described["env_file"], str(env.resolve()))
        self.assertEqual(described["JUDGE"]["model"], "judge-from-file")


if __name__ == "__main__":
    unittest.main()
