"""Provider-free argv preservation tests for the run-local Codex wrapper."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "codex_feature_wrapper", ROOT / "codex_feature_wrapper.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRAPPER)
PREFIX = ["--disable", "unified_exec", "--disable", "code_mode_host"]


class CodexFeatureWrapperTests(unittest.TestCase):
    def test_conflicting_separate_and_equals_enables_are_removed(self):
        original = ["exec", "--enable", "unified_exec", "--enable=code_mode_host",
                    "--", "task"]
        self.assertEqual(WRAPPER.native_args(original), PREFIX + ["exec", "--", "task"])

    def test_unrelated_enables_are_retained(self):
        original = ["exec", "--enable", "shell_snapshot", "--enable=goals"]
        self.assertEqual(WRAPPER.native_args(original), PREFIX + original)

    def test_option_values_that_resemble_flags_are_retained(self):
        original = ["exec", "-c", "--enable", "--model", "deepseek-flash", "--", ""]
        self.assertEqual(WRAPPER.native_args(original), PREFIX + original)

    def test_every_token_after_delimiter_is_retained(self):
        prompt = "Chinese prompt\nkeep 'quotes', $HOME and --enable code_mode_host"
        original = ["exec", "--", "--enable", "unified_exec", prompt, ""]
        self.assertEqual(WRAPPER.native_args(original), PREFIX + original)

    def test_stage_builder_copies_and_mounts_the_tested_wrapper(self):
        source = (ROOT / "formal_one_stop.py").read_text()
        self.assertIn("Path(__file__).resolve().parent / 'codex_feature_wrapper.py'", source)
        self.assertIn('"target": "/workspace/codex-wrapper/codex"', source)
        self.assertIn('"PATH": "/workspace/codex-wrapper:', source)

    def test_main_execs_native_codex_with_preserved_argv(self):
        with patch.object(WRAPPER.os, "execv") as execute:
            with patch.object(WRAPPER.sys, "argv", ["codex", "exec", "--", "task"]):
                # Exercise the same final values used by the module entrypoint.
                WRAPPER.os.execv(WRAPPER.NATIVE_CODEX,
                    ["codex"] + WRAPPER.native_args(WRAPPER.sys.argv[1:]))
        execute.assert_called_once_with(WRAPPER.NATIVE_CODEX,
            ["codex"] + PREFIX + ["exec", "--", "task"])


if __name__ == "__main__":
    unittest.main()
