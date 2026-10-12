"""Per-case and per-run tokens for every broker role that the agent under test does not use (stdlib unittest; docker,
providers and controllers are faked, no model calls).

The agent under test keeps its public placeholder (runtime-only-placeholder / broker-only-placeholder in the
Optimization runtimes, broker-only-placeholder and search-placeholder for Creation Candidates). Every other role takes
a random token that only its own evaluator-side client receives, and the public constants judge-only-placeholder and
stats-only-placeholder are refused:

* responses_broker.py (tau3, PinchBench, TerminalBench): judge and stats tokens from the broker's environment; without
  a judge token there is no judge role; it does not start without a private stats token.
* browsecomp_broker.py: the stats and finalize token from the sidecar's environment, set by the adapter.
* tau3 and PinchBench controllers: the tokens reach the broker (and PinchBench's grader) by name in the environment,
  never the runtime container of the agent under test or a command line; a judge call is never scored when the
  grader cannot have made it (tau3 at all, PinchBench before grading).
* TerminalBench: the live broker is published on the host loopback only and has no judge role.
* Creation runner: one random client token per provider broker (builder to Codex, runtime to the evaluator broker,
  judge to judge.env); Optimization runner: the builder token to Codex.
* Creation `stop` removes every secret file a run wrote, as the one_stop exit wrapper does.
* AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS and AGENTSWE_EVALUATOR_PROXY_URL reach the provider broker containers;
  AGENTSWE_BUILDER_CONTEXT_WINDOW reaches the Codex config generator.
"""
from __future__ import annotations

import http.server
import importlib
import importlib.util
import itertools
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
NATIVE = ROOT / "runners" / "optimization" / "optimization_native"

from agentswe import config, util  # noqa: E402
from agentswe.runners import creation_harbor_v1 as creation  # noqa: E402
from agentswe.runners import optimization_native_v1 as opt  # noqa: E402

JUDGE_ENV = "AGENTSWE_BROKER_JUDGE_TOKEN"
STATS_ENV = "AGENTSWE_BROKER_STATS_TOKEN"
CLIENT_ENV = "AGENTSWE_BROKER_CLIENT_TOKEN"
PUBLIC = {"broker-only-placeholder", "runtime-only-placeholder", "judge-only-placeholder", "stats-only-placeholder",
          "search-placeholder"}
FAKE_KEY = "sk-fake-provider-key-" + "0" * 10  # a test constant, never a real key


CASE_JUDGE, CASE_STATS = secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def private(token) -> bool:
    return isinstance(token, str) and len(token) >= 32 and token not in PUBLIC


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class FakeUpstream:
    """A Responses provider and search API that answers every POST."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                outer.requests.append({"path": self.path,
                                       "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                data = json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                                   "usage": {"total_tokens": 5}, "organic": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class NativeBroker:
    """One of the Optimization native brokers as a subprocess, with the given role tokens in its environment."""

    def __init__(self, script: str, credentials: dict[str, str], tokens: dict[str, str]) -> None:
        self.dir = tempfile.TemporaryDirectory()
        cred = Path(self.dir.name) / "evaluator.env"
        cred.write_text("".join(f"{k}={v}\n" for k, v in credentials.items()))
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}
        env.update(tokens)
        self.argv = [sys.executable, str(NATIVE / script), "--credential-file", str(cred), "--bind", "127.0.0.1",
                     "--port", str(self.port)]
        self.env = env
        self.proc = subprocess.Popen(self.argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(200):
            if self.proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.close()
        raise RuntimeError("broker did not start")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stderr.close()
        self.dir.cleanup()


def start_refused(script: str, credentials: dict[str, str], tokens: dict[str, str]) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        cred = Path(tmp) / "evaluator.env"
        cred.write_text("".join(f"{k}={v}\n" for k, v in credentials.items()))
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}
        env.update(tokens)
        return subprocess.run([sys.executable, str(NATIVE / script), "--credential-file", str(cred),
                               "--bind", "127.0.0.1", "--port", str(free_port())],
                              env=env, capture_output=True, text=True, timeout=30)


# ---------------------------------------------------------------------------------------------------------------
# Optimization native brokers
# ---------------------------------------------------------------------------------------------------------------
class ResponsesBrokerRoles(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream()
        self.creds = {"AGENTSWE_RUNTIME_API_KEY": FAKE_KEY, "AGENTSWE_RUNTIME_RESPONSES_URL": self.up.url + "/v1/responses"}

    def tearDown(self):
        self.up.close()

    def test_public_judge_and_stats_placeholders_are_refused(self):
        judge, stats = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        broker = NativeBroker("responses_broker.py", self.creds, {JUDGE_ENV: judge, STATS_ENV: stats})
        try:
            refused = [request(broker.url("/v1/responses"), t, {"input": "x"})[0]
                       for t in ("judge-only-placeholder", "stats-only-placeholder", stats, "")]
            self.assertEqual(refused, [401, 401, 401, 401])
            self.assertEqual(request(broker.url("/stats"), "stats-only-placeholder")[0], 401)
            self.assertEqual(request(broker.url("/stats"), judge)[0], 401)
            # the agent under test keeps its placeholders
            for token in ("runtime-only-placeholder", "broker-only-placeholder"):
                self.assertEqual(request(broker.url("/v1/responses"), token, {"input": "x"})[0], 200)
            self.assertEqual(request(broker.url("/v1/responses"), judge, {"input": "x"})[0], 200)
            status, stats_value = request(broker.url("/stats"), stats)
        finally:
            broker.close()
        self.assertEqual(status, 200)
        self.assertEqual((stats_value["runtime"]["calls"], stats_value["judge"]["calls"]), (2, 1))
        self.assertTrue(stats_value["judge_role_enabled"])
        self.assertEqual(stats_value["unauthorized_requests"], 6)
        self.assertEqual(len(self.up.requests), 3)

    def test_without_a_judge_token_there_is_no_judge_role(self):
        stats = secrets.token_urlsafe(32)
        broker = NativeBroker("responses_broker.py", self.creds, {STATS_ENV: stats})
        try:
            for token in ("judge-only-placeholder", secrets.token_urlsafe(32)):
                self.assertEqual(request(broker.url("/v1/responses"), token, {"input": "x"})[0], 401)
            status, value = request(broker.url("/stats"), stats)
        finally:
            broker.close()
        self.assertEqual(status, 200)
        self.assertFalse(value["judge_role_enabled"])
        self.assertEqual(value["judge"]["calls"], 0)
        self.assertEqual(self.up.requests, [])

    def test_no_start_without_private_role_tokens(self):
        for tokens in ({}, {STATS_ENV: "stats-only-placeholder"}, {STATS_ENV: "short"},
                       {STATS_ENV: secrets.token_urlsafe(32), JUDGE_ENV: "judge-only-placeholder"}):
            done = start_refused("responses_broker.py", self.creds, tokens)
            self.assertNotEqual(done.returncode, 0, tokens)
            self.assertIn("token", done.stderr)


class BrowseCompStatsToken(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream()
        self.creds = {"AGENTSWE_RUNTIME_API_KEY": FAKE_KEY, "AGENTSWE_RUNTIME_RESPONSES_URL": self.up.url + "/v1/responses",
                      "AGENTSWE_SEARCH_API_KEY": "fake-search-key", "AGENTSWE_SEARCH_BASE_URL": self.up.url,
                      "AGENTSWE_SEARCH_WIRE": "serper"}

    def tearDown(self):
        self.up.close()

    def test_stats_need_the_sidecar_token_and_the_candidate_keeps_its_placeholder(self):
        stats = secrets.token_urlsafe(32)
        broker = NativeBroker("browsecomp_broker.py", self.creds, {STATS_ENV: stats})
        try:
            self.assertEqual(request(broker.url("/stats"), "stats-only-placeholder")[0], 401)
            self.assertEqual(request(broker.url("/stats"), "broker-only-placeholder")[0], 401)
            self.assertEqual(request(broker.url("/v1/responses"), "broker-only-placeholder",
                                     {"input": [{"role": "user", "content": "hi"}]})[0], 200)
            status, value = request(broker.url("/stats"), stats)
        finally:
            broker.close()
        self.assertEqual(status, 200)
        self.assertEqual(value["resources"]["model"]["calls"], 1)

    def test_no_start_without_the_token(self):
        for tokens in ({}, {STATS_ENV: "stats-only-placeholder"}):
            self.assertNotEqual(start_refused("browsecomp_broker.py", self.creds, tokens).returncode, 0)

    def test_the_adapter_gives_each_sidecar_its_own_token(self):
        spec = importlib.util.spec_from_file_location("fx_opt_adapter", ROOT / "runners" / "optimization" / "adapter.py")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "evaluator.env").write_text("AGENTSWE_RUNTIME_MODEL=paper-model\n")
            composes = [adapter.compose_browsecomp_candidate(tmp / "candidate", tmp / "dev_001", "dev_001", "d" * 64,
                                                             tmp / "evaluator.env") for _ in range(2)]
        tokens = [c["services"]["browsecomp-broker"]["environment"][STATS_ENV] for c in composes]
        self.assertTrue(all(private(t) for t in tokens))
        self.assertNotEqual(tokens[0], tokens[1])
        main = composes[0]["services"]["main"]
        self.assertNotIn(tokens[0], json.dumps(main))
        self.assertEqual(main["environment"]["OPENAI_API_KEY"], "broker-only-placeholder")


# ---------------------------------------------------------------------------------------------------------------
# tau3 and PinchBench controllers, docker faked
# ---------------------------------------------------------------------------------------------------------------
def mount(command: list[str], target: str) -> Path | None:
    for flag, value in zip(command, command[1:]):
        if flag == "-v" and value.endswith(":" + target):
            return Path(value[: -len(target) - 1])
    return None


class FakeDocker:
    """`run(command, timeout=, env=)` of a native controller: records each docker command and the environment the
    docker CLI got; containers write what the controller expects into their /output mount."""

    def __init__(self, stats: list[dict], on_container) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.stats = stats
        self.on_container = on_container

    def __call__(self, command, *, timeout=60, env=None):
        self.calls.append((list(command), env))
        if command[:2] == ["docker", "exec"]:
            value = self.stats.pop(0) if len(self.stats) > 1 else self.stats[0]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        if command[:3] == ["docker", "run", "-d"]:
            return subprocess.CompletedProcess(command, 0, "cid\n", "")
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(command, 0, self.on_container(command, mount(command, "/output")) or "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def broker_call(self) -> tuple[list[str], dict]:
        return next((c, e) for c, e in self.calls if c[:3] == ["docker", "run", "-d"])


def load_native(name: str):
    sys.path.insert(0, str(NATIVE))
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def stats(runtime_calls: int, judge_calls: int) -> dict:
    return {"runtime": {"calls": runtime_calls, "tokens": 10, "failures": 0, "budget_exceeded": False},
            "judge": {"calls": judge_calls, "tokens": 0, "failures": 0, "budget_exceeded": False}}


def stats_script_header(module, token: str) -> str:
    """Run the controller's in-container stats script with urlopen faked; return the Authorization it sends."""
    seen = {}

    class Reply:
        def read(self):
            return b"{}"

    def urlopen(req, *a, **k):
        seen["auth"] = req.get_header("Authorization")
        return Reply()

    with mock.patch.dict(os.environ, {STATS_ENV: token}), mock.patch("urllib.request.urlopen", urlopen), \
            mock.patch("builtins.print"):
        exec(compile(module.BROKER_STATS_SCRIPT, "stats", "exec"), {})
    return seen["auth"]


class Tau3Controller(unittest.TestCase):
    def run_case(self, broker_stats: list[dict]):
        m = load_native("tau3_controller")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        predictions = tmp / "predictions.jsonl"
        predictions.write_text(json.dumps({"id": "dev_001", "agent": {"strategy_prompt": "be brief"}}) + "\n")

        def container(command, output):
            if "/runtime.py" in command:
                (output / "native_result.json").write_text(json.dumps({"official_evaluation": True, "score": 100}))
                (output / "trajectory.json").write_text("[]")

        docker = FakeDocker(broker_stats, container)
        argv = ["tau3_controller.py", "--case-id", "dev_001", "--domain", "retail", "--task-id", "7",
                "--predictions", str(predictions), "--credential-file", str(tmp / "evaluator.env"),
                "--output-dir", str(tmp / "out")]
        with mock.patch.object(m, "run", docker), \
                mock.patch.object(m, "prediction", return_value={"id": "dev_001", "agent": {}}), \
                mock.patch.object(m, "allocate_network_subnet", return_value="198.18.0.0/28"), \
                mock.patch.object(m, "create_network", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                mock.patch.object(m, "remove_docker_object", return_value=True), \
                mock.patch.object(m, "sha256", return_value="0" * 64), \
                mock.patch.object(m.time, "sleep"), mock.patch.object(sys, "argv", argv):
            self.assertEqual(m.main(), 0)
        return m, docker, json.loads((tmp / "out" / "native_result.json").read_text())

    def test_stats_token_reaches_only_the_broker(self):
        m, docker, result = self.run_case([stats(3, 0)])
        command, env = docker.broker_call()
        token = env[STATS_ENV]
        self.assertTrue(private(token))
        self.assertEqual(command[command.index(STATS_ENV) - 1], "-e")  # by name: the value is on no command line
        self.assertNotIn(JUDGE_ENV, command)  # tau3 has no judge client: no judge role
        self.assertNotIn(JUDGE_ENV, env)
        for argv, call_env in docker.calls:
            self.assertNotIn(token, " ".join(argv))
            if argv is not command:
                self.assertTrue(call_env is None or STATS_ENV not in call_env, argv[:3])
        runtime = next(c for c, _ in docker.calls if "/runtime.py" in c)
        self.assertIn("OPENAI_API_KEY=broker-only-placeholder", runtime)  # the agent's runtime token is unchanged
        self.assertEqual(stats_script_header(m, token), f"Bearer {token}")
        self.assertEqual(result["score"], 100)

    def test_a_judge_call_is_never_scored(self):
        _, docker, result = self.run_case([stats(3, 1)])
        self.assertTrue(result["infrastructure_failure"])
        self.assertEqual({a.get("error") for a in result["provider_attempts"]}, {"judge_role_used_without_judge"})
        self.assertEqual(len(docker.stats), 1)


class PinchBenchController(unittest.TestCase):
    def run_case(self, broker_stats: list[dict]):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        with mock.patch.dict(os.environ, {"AGENTSWE_HOME": str(tmp)}):
            m = load_native("pinchbench_controller")

        def container(command, output):
            if "--help" in command:
                return "Usage: openclaw agent [options]\n  --local\n"
            if "/runtime.py" in command:
                output.mkdir(parents=True, exist_ok=True)
                (output / "execution_result.json").write_text(json.dumps({"status": "success"}))
            if "/grader.py" in command:
                (output / "grade.json").write_text(json.dumps({"score": 0.5, "grading_type": "llm_judge"}))
            return ""

        docker = FakeDocker(broker_stats, container)
        argv = ["pinchbench_controller.py", "--case-id", "dev_001", "--task-id", "task_x",
                "--predictions", str(tmp / "p.jsonl"), "--credential-file", str(tmp / "evaluator.env"),
                "--output-dir", str(tmp / "out")]
        task = types.SimpleNamespace(prompt="do it", timeout_seconds=60)
        with mock.patch.object(m, "run", docker), \
                mock.patch.object(m, "load_task", return_value=task), \
                mock.patch.object(m, "load_prediction", return_value=({"id": "dev_001"}, {})), \
                mock.patch.object(m, "prepare_workspace"), mock.patch.object(m, "openclaw_config", return_value={}), \
                mock.patch.object(m, "sha256", return_value="0" * 64), \
                mock.patch.object(m, "allocate_network_subnets", return_value=("100.64.0.0/28", "100.64.0.16/28")), \
                mock.patch.object(m, "create_network", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                mock.patch.object(m, "release_case_resources"), mock.patch.object(m.time, "sleep"), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(m.main(), 0)
        return m, docker, json.loads((tmp / "out" / "native_result.json").read_text())

    def test_judge_token_reaches_only_the_broker_and_the_grader(self):
        m, docker, result = self.run_case([stats(4, 0), stats(4, 1)])
        command, env = docker.broker_call()
        judge, stats_token = env[JUDGE_ENV], env[STATS_ENV]
        self.assertTrue(private(judge) and private(stats_token) and judge != stats_token)
        for name in (JUDGE_ENV, STATS_ENV):
            self.assertEqual(command[command.index(name) - 1], "-e")
        grader, grader_env = next((c, e) for c, e in docker.calls if "/grader.py" in c)
        self.assertEqual(grader_env[JUDGE_ENV], judge)
        self.assertNotIn(STATS_ENV, grader_env)
        self.assertEqual(grader[grader.index(JUDGE_ENV) - 1], "-e")
        for argv, call_env in docker.calls:
            self.assertNotIn(judge, " ".join(argv))
            self.assertNotIn(stats_token, " ".join(argv))
            if "/runtime.py" in argv or "--help" in argv:  # the OpenClaw runtime, which runs the agent under test
                self.assertTrue(call_env is None or not ({JUDGE_ENV, STATS_ENV} & set(call_env)))
                self.assertNotIn(JUDGE_ENV, argv)
        self.assertEqual(stats_script_header(m, stats_token), f"Bearer {stats_token}")
        self.assertEqual(result["score"], 50)

    def test_a_judge_call_before_grading_is_never_scored(self):
        _, docker, result = self.run_case([stats(4, 1)])
        self.assertTrue(result["infrastructure_failure"])
        self.assertEqual({a.get("error") for a in result["provider_attempts"]}, {"judge_role_used_before_grade"})
        self.assertFalse(any("/grader.py" in c for c, _ in docker.calls))


class PinchBenchGrader(unittest.TestCase):
    def test_the_judge_sends_the_case_token(self):
        m = load_native("pinchbench_grader")
        up = FakeUpstream()
        seen = []
        original = urllib.request.urlopen

        def urlopen(req, *a, **k):
            seen.append(req.get_header("Authorization"))
            return original(req, *a, **k)

        try:
            with mock.patch.object(m.urllib.request, "urlopen", urlopen):
                reply = m.BrokerJudge(up.url + "/v1", CASE_JUDGE)(prompt="p", model="x")
        finally:
            up.close()
        self.assertEqual(reply["status"], "success")
        self.assertEqual(seen, ["Bearer " + CASE_JUDGE])

    def test_the_grader_needs_the_token_and_takes_it_out_of_its_environment(self):
        source = (NATIVE / "pinchbench_grader.py").read_text()
        self.assertNotIn("judge-only-placeholder", source)
        self.assertIn("os.environ.pop(JUDGE_TOKEN_ENV", source)


# ---------------------------------------------------------------------------------------------------------------
# TerminalBench live broker
# ---------------------------------------------------------------------------------------------------------------
class TerminalBenchLiveBroker(unittest.TestCase):
    def setUp(self):
        self.m = load_native("terminalbench_controller")

    def test_published_on_the_host_loopback_only(self):
        command = self.m.live_broker_command("agentswe-tb-broker-x", Path("/tmp/evaluator.env"))
        self.assertEqual(command[command.index("-p") + 1], "127.0.0.1::8080")
        self.assertEqual(command.count("-p"), 1)
        self.assertFalse(any("0.0.0.0" in part for part in command))
        self.assertNotIn("0.0.0.0", (NATIVE / "terminalbench_controller.py").read_text())
        self.assertEqual(command[command.index(STATS_ENV) - 1], "-e")
        self.assertNotIn(JUDGE_ENV, command)  # scored by the task's tests: no judge role
        self.assertIn("--chat-role", command)

    def test_stats_read_sends_the_case_token(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append(self.headers.get("Authorization"))
                data = json.dumps(stats(2, 0)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            value = self.m.read_broker_stats(server.server_address[1], CASE_STATS)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen, ["Bearer " + CASE_STATS])
        self.assertEqual(self.m.broker_outcome(value), "ok")

    def test_a_judge_call_is_never_scored(self):
        self.assertEqual(self.m.broker_outcome(stats(2, 1)), "judge_role_used")


# ---------------------------------------------------------------------------------------------------------------
# Creation and Optimization runners, docker faked
# ---------------------------------------------------------------------------------------------------------------
class FakeHost:
    """util.run / util.out / Popen of a runner: docker and the generator succeed; every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict | None]] = []
        self.popen: dict = {}
        self.ids = itertools.count(1)

    def run(self, cmd, *, env=None, check=True, capture=True, timeout=None, cwd=None):
        self.calls.append((list(cmd), env))
        return subprocess.CompletedProcess(cmd, 0, f"cid{next(self.ids)}\n")

    def Popen(self, args, **kw):  # noqa: N802
        self.popen = {"args": args, **kw}
        return types.SimpleNamespace(pid=999_999_999)

    def broker(self, role: str) -> tuple[list[str], dict]:
        return next((c, e) for c, e in self.calls
                    if c[:2] == ["docker", "run"] and "agentswe_broker" in c and c[c.index("--role") + 1] == role)

    def generator(self) -> list[str]:
        return next(c for c, _ in self.calls if "--model" in c and "--out-dir" in c)


def make_cfg(home: Path, **values: str) -> config.Config:
    base = dict(config.LITE_DEFAULTS)
    base.update({"AGENTSWE_HOME": str(home), "AGENTSWE_DEFAULT_API_KEY": FAKE_KEY, "AGENTSWE_BROKER_HOST": "172.17.0.1",
                 "AGENTSWE_SEARCH_BASE_URL": "https://search.invalid", "AGENTSWE_SEARCH_API_KEY": "fake-search-key"})
    base.update(values)
    return config.Config(values=base, env_file=None)


def dotenv(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


class CreationRunner(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.home)]))
        self.task = types.SimpleNamespace(
            id="creation/web-research-report", short="web", family="creation", dir=self.home / "task",
            data={"protocol": {"dev_cases": ["dev_001"], "heldout_cases": ["test_001"], "max_dev_rounds": 2},
                  "smoke": {}, "services": ["search"], "envs": ["web-env"], "runner_config": {}})
        util.write_json(self.home / "state" / "setup.json", {"tasks": {self.task.id: {}}})
        self.labels = itertools.count()

    def launch(self, **values: str):
        cfg = make_cfg(self.home, **values)
        host = FakeHost()

        def certificates(out: Path) -> dict:
            out.mkdir(parents=True, exist_ok=True)
            for name in ("ca.pem", "cert.pem", "key.pem", "ca-bundle.pem"):
                (out / name).write_text(name)
            return {"ca": str(out / "ca.pem"), "cert": str(out / "cert.pem"), "key": str(out / "key.pem"),
                    "ca_bundle": str(out / "ca-bundle.pem"), "ca_sha256": "0" * 64, "cert_sha256": "0" * 64,
                    "ca_bundle_sha256": "0" * 64, "ca_subject_hash": "0123abcd"}

        ports = itertools.count(20000)
        with mock.patch.object(util, "run", host.run), mock.patch.object(util, "out", return_value=""), \
                mock.patch.object(creation, "wait_healthy"), mock.patch.object(creation, "free_port", lambda h: next(ports)), \
                mock.patch.object(creation, "staged", lambda cfg, task, sub: task.dir / sub), \
                mock.patch.object(creation, "make_search_certificates", certificates), \
                mock.patch.object(creation.subprocess, "Popen", host.Popen):
            manifest = creation.run(cfg, self.task, builder="codex", seed=1, smoke=False, label=f"t{next(self.labels)}")
        return cfg, host, manifest

    def test_each_provider_broker_accepts_only_its_own_clients_token(self):
        cfg, host, manifest = self.launch()
        secrets_dir = cfg.home / "secrets" / manifest["run_id"]
        tokens = {}
        for role in ("builder", "runtime", "judge"):
            command, env = host.broker(role)
            tokens[role] = env[CLIENT_ENV]
            self.assertEqual(command[command.index(CLIENT_ENV) - 1], "-e")  # by name, not on the command line
            self.assertNotIn("--client-token", command)
            self.assertTrue(private(env[STATS_ENV]))
        self.assertTrue(all(private(t) for t in tokens.values()))
        self.assertEqual(len(set(tokens.values())), 3)
        one_stop_env = host.popen["env"]
        judge_env, evaluator_env = dotenv(secrets_dir / "judge.env"), dotenv(secrets_dir / "evaluator-broker.env")
        candidate_env = (secrets_dir / "candidate.env").read_text()
        # each client gets its own token
        self.assertEqual(one_stop_env["AGENTSWE_BUILDER_API_KEY"], tokens["builder"])  # Codex
        self.assertEqual(evaluator_env, {"AGENTSWE_RUNTIME_API_KEY": tokens["runtime"]})  # the evaluator broker
        self.assertEqual(judge_env, {"AGENTSWE_JUDGE_API_KEY": tokens["judge"]})  # the Result judge (Eval jobs)
        # ... and no other token
        self.assertEqual(candidate_env, "GATEWAY_API_KEY=broker-only-placeholder\nSERPER_TOKEN=search-placeholder\n")
        everything_else = json.dumps(manifest) + json.dumps(host.popen["args"]) + json.dumps([c for c, _ in host.calls])
        everything_else += (cfg.home / "runs" / "creation" / f"{manifest['run_id']}.one_stop.log").read_text()
        for role, token in tokens.items():
            self.assertNotIn(token, everything_else, role)
            self.assertNotIn(token, candidate_env)
        self.assertNotIn(tokens["judge"], json.dumps(one_stop_env))
        self.assertNotIn(tokens["runtime"], json.dumps(one_stop_env))
        self.assertNotIn(tokens["builder"], json.dumps([judge_env, evaluator_env]))
        # the Candidate-facing evaluator broker and the search broker keep the Candidate's placeholders
        evaluator = next(c for c, _ in host.calls if "/broker.py" in c)
        self.assertIn(f"{secrets_dir / 'evaluator-broker.env'}:/run/secrets/agentswe.env:ro", evaluator)
        search, search_env = host.broker("search")
        self.assertEqual(search[search.index("--client-token") + 1], "search-placeholder")
        self.assertTrue(private(search_env[STATS_ENV]))
        self.assertNotIn(CLIENT_ENV, search_env)

    def test_provider_broker_settings_pass_through(self):
        _, host, _ = self.launch(AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS="4096",
                                 AGENTSWE_EVALUATOR_PROXY_URL="http://127.0.0.1:7890")
        for role in ("builder", "runtime", "judge", "search"):
            command, _ = host.broker(role)
            self.assertIn("AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS=4096", command)
            self.assertIn("AGENTSWE_EVALUATOR_PROXY_URL=http://127.0.0.1:7890", command)
            self.assertLess(command.index("AGENTSWE_EVALUATOR_PROXY_URL=http://127.0.0.1:7890"), command.index("python3"))
        _, host, _ = self.launch()
        for role in ("builder", "runtime", "judge", "search"):
            command, _ = host.broker(role)
            self.assertFalse(any(part.startswith(("AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS=",
                                                  "AGENTSWE_EVALUATOR_PROXY_URL=")) for part in command))

    def test_builder_context_window_reaches_the_generator(self):
        _, host, _ = self.launch(AGENTSWE_BUILDER_CONTEXT_WINDOW="200000")
        generator = host.generator()
        self.assertEqual(generator[generator.index("--context-window") + 1], "200000")
        _, host, _ = self.launch()
        self.assertNotIn("--context-window", host.generator())
        with self.assertRaises(SystemExit):
            self.launch(AGENTSWE_BUILDER_CONTEXT_WINDOW="large")

    def test_stop_and_the_exit_wrapper_remove_every_secret_file(self):
        cfg, host, manifest = self.launch()
        secrets_dir = cfg.home / "secrets" / manifest["run_id"]
        written = sorted(p.relative_to(secrets_dir).as_posix() for p in secrets_dir.rglob("*"))
        for name in ("search.key", "search-front/key.pem", "judge.env", "evaluator-broker.env", "candidate.env",
                     "builder.key", "runtime.key", "judge.key"):
            self.assertIn(name, written)
        # the exit wrapper's cleanup, run as the wrapper runs it
        wrapper = host.popen["args"][2]
        self.assertIn(creation.secrets_cleanup_shell(secrets_dir), wrapper)
        copy = cfg.home / "copy"
        subprocess.run(["cp", "-a", str(secrets_dir), str(copy)], check=True)
        subprocess.run(["bash", "-c", creation.secrets_cleanup_shell(copy)], check=False)
        self.assertFalse(copy.exists())
        # `agentswe stop` after SIGTERM ended the wrapper before its cleanup
        with mock.patch.object(util, "run", host.run), mock.patch.object(util, "out", return_value=""), \
                mock.patch.object(util, "pid_alive", return_value=False):
            creation.stop(cfg, manifest)
        self.assertFalse(secrets_dir.exists(), sorted(secrets_dir.rglob("*")) if secrets_dir.exists() else None)


class OptimizationRunner(unittest.TestCase):
    def test_codex_gets_the_builder_brokers_token(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(home)]))
        task = types.SimpleNamespace(
            id="optimization/tau3-retail", short="tau3", family="optimization", dir=home / "task",
            data={"protocol": {"max_dev_rounds": 2}, "services": [],
                  "runner_config": {"benchmark_name": "bench", "python_runtime_env": "py"}})
        util.write_json(home / "state" / "setup.json", {"tasks": {task.id: {}}})
        (home / "task" / "benchmark").mkdir(parents=True)
        cfg = make_cfg(home, AGENTSWE_BUILDER_CONTEXT_WINDOW="131072")
        host = FakeHost()
        with mock.patch.object(util, "run", host.run), mock.patch.object(util, "out", return_value=""), \
                mock.patch.object(creation, "wait_healthy"), mock.patch.object(opt, "free_port", lambda h: 20001), \
                mock.patch.object(opt, "staged", lambda cfg, task, sub: task.dir / sub), \
                mock.patch.object(opt.subprocess, "Popen", host.Popen):
            manifest = opt.run(cfg, task, builder="codex", seed=1, smoke=False, label=None)
        command, env = host.broker("builder")
        token = env[CLIENT_ENV]
        self.assertTrue(private(token))
        self.assertEqual(command[command.index(CLIENT_ENV) - 1], "-e")
        self.assertEqual(host.popen["env"]["AGENTSWE_BUILDER_API_KEY"], token)
        evaluator = (home / "secrets" / manifest["run_id"] / "evaluator.env").read_text()
        self.assertNotIn(token, evaluator + json.dumps(manifest) + json.dumps([c for c, _ in host.calls]))
        generator = host.generator()
        self.assertEqual(generator[generator.index("--context-window") + 1], "131072")


if __name__ == "__main__":
    unittest.main()
