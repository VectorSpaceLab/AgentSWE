import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import code_judge_runner as runner


def completed(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class OwnedCodeContainerTests(unittest.TestCase):
    def test_unbound_capture_cannot_replace_unknown_transport_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            original = {'provider_usage': {'total_tokens': None, 'input_tokens': None,
                        'output_tokens': None, 'usage_complete': False},
                        'code_score': None, 'contract_valid': False}
            (output / 'code_score_contract.json').write_text(json.dumps(original))
            (output / 'provider_usage_capture.json').write_text(json.dumps({
                'observations': 2, 'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}))
            runner.record_usage_capture(output)
            value = json.loads((output / 'code_score_contract.json').read_text())
            self.assertEqual(value['provider_usage'], original['provider_usage'])
            self.assertIsNone(value['code_score'])
            self.assertFalse(value['contract_valid'])
            self.assertFalse(value['provider_usage_capture']['authoritative'])

    def test_ownership_mismatch_never_removes(self):
        observed = json.dumps([{"Id": "a" * 64, "Config": {"Labels": {"agentswe.code-judge.owner": "foreign"}}}])
        with patch.object(runner.subprocess, "run", return_value=completed(stdout=observed)) as invoke:
            value = runner.cleanup_owned_container("named-target", "ours")
        self.assertFalse(value["complete"])
        self.assertEqual(invoke.call_count, 1)

    def test_owned_exact_id_is_removed_and_absence_verified(self):
        observed = json.dumps([{"Id": "a" * 64, "Config": {"Labels": {"agentswe.code-judge.owner": "ours"}}}])
        with patch.object(runner.subprocess, "run", side_effect=[completed(stdout=observed), completed(),
                completed(1, stderr="Error: No such container: " + "a" * 64)]) as invoke:
            value = runner.cleanup_owned_container("named-target", "ours")
        self.assertTrue(value["complete"])
        self.assertEqual(invoke.call_args_list[1].args[0], ["docker", "container", "rm", "-f", "a" * 64])

    def test_unavailable_docker_is_not_successful_cleanup(self):
        with patch.object(runner.subprocess, "run", return_value=completed(1, stderr="Cannot connect to Docker daemon")) as invoke:
            value = runner.cleanup_owned_container("named-target", "ours")
        self.assertFalse(value["complete"])
        self.assertEqual(invoke.call_count, 1)

    def exercise(self, outcome):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = ["docker", "run", "--rm", "--name", "agentswe-test", "image", "true"]
            with patch.object(runner.subprocess, "run", side_effect=outcome) as invoke, \
                    patch.object(runner, "cleanup_owned_container", return_value={"complete": True}) as cleanup:
                result = runner.run_owned_container(command, root, "preflight", 2)
                receipt = json.loads((root / "code_preflight_lifecycle.json").read_text())
            self.assertEqual(cleanup.call_count, 1)
            called = invoke.call_args.args[0]
            self.assertEqual(cleanup.call_args.args[0], called[called.index("--name") + 1])
            self.assertIn("agentswe.code-judge.owner=" + cleanup.call_args.args[1], called)
            self.assertTrue(receipt["cleanup"]["complete"])
            return result, receipt

    def test_timeout_always_cleans_and_preserves_output(self):
        result, receipt = self.exercise(subprocess.TimeoutExpired(["docker"], 2, output=b"partial", stderr=b"pending"))
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial")
        self.assertEqual(receipt["state"], "outer_timeout_infrastructure_invalid")

    def test_sigterm_always_cleans(self):
        result, receipt = self.exercise(runner.RunnerInterrupted(signal.SIGTERM))
        self.assertEqual(result.returncode, 143)
        self.assertEqual(receipt["state"], "interrupted_infrastructure_invalid")

    def test_no_reentry_overwrites_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "code_preflight_lifecycle.json").write_text("original")
            with patch.object(runner.subprocess, "run") as invoke, self.assertRaises(FileExistsError):
                runner.run_owned_container(["docker", "run", "--name", "owned", "image"], root, "preflight", 2)
            invoke.assert_not_called()
            self.assertEqual((root / "code_preflight_lifecycle.json").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
