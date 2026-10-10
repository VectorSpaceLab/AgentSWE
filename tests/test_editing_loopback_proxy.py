"""The bundled loopback CONNECT proxy tunnels to a local HTTP server; the probe tells a free port, a CONNECT proxy and
another service apart; `agentswe run` uses a listener already there, starts the bundled proxy when the port is free,
and stops when the port is held by something else; formal runs pass the configured Builder URL (stdlib unittest, no
docker, no systemd, no network beyond 127.0.0.1)."""
from __future__ import annotations

import asyncio
import http.server
import importlib.util
import socket
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import doctor, loopback_proxy  # noqa: E402
from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402

BODY = b"through the tunnel\n"


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


def http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server) -> None:
    server.shutdown()
    server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BundledProxy:
    """loopback_proxy.serve on an ephemeral port, in its own event loop thread."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        ready, bound = threading.Event(), []

        def run():
            asyncio.set_event_loop(self.loop)
            self.task = self.loop.create_task(loopback_proxy.serve("127.0.0.1", 0, ready, bound))
            try:
                self.loop.run_until_complete(self.task)
            except asyncio.CancelledError:
                pass
            self.loop.run_until_complete(asyncio.sleep(0.1))  # let the closing tunnels finish

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not ready.wait(10):
            raise RuntimeError("proxy did not start")
        self.port = bound[0]
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(5)
        self.loop.close()


def connect(port: int, request: bytes) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(request)
    return s


def read_head(s: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        block = s.recv(4096)
        if not block:
            break
        data += block
    return data


class ProxyTunnel(unittest.TestCase):
    def test_connect_tunnels_an_http_request(self):
        web = http_server()
        try:
            with BundledProxy() as proxy:
                authority = f"127.0.0.1:{web.server_address[1]}"
                with connect(proxy.port, f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()) as s:
                    head = read_head(s)
                    self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
                    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    reply = b""
                    while True:
                        block = s.recv(4096)
                        if not block:
                            break
                        reply += block
                self.assertTrue(reply.startswith(b"HTTP/1.0 200") or reply.startswith(b"HTTP/1.1 200"), reply[:40])
                self.assertTrue(reply.endswith(BODY), reply[-40:])
        finally:
            stop(web)

    def test_bytes_sent_with_the_head_reach_the_target(self):
        web = http_server()
        try:
            with BundledProxy() as proxy:
                authority = f"127.0.0.1:{web.server_address[1]}"
                request = (f"CONNECT {authority} HTTP/1.1\r\n\r\n"
                           "GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").encode()
                with connect(proxy.port, request) as s:
                    reply = b""
                    while True:
                        block = s.recv(4096)
                        if not block:
                            break
                        reply += block
                self.assertTrue(reply.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n"), reply[:60])
                self.assertTrue(reply.endswith(BODY))
        finally:
            stop(web)

    def test_refuses_other_methods_and_reports_unreachable_targets(self):
        with BundledProxy() as proxy:
            with connect(proxy.port, b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n") as s:
                self.assertTrue(read_head(s).startswith(b"HTTP/1.1 405"))
            closed = free_port()
            with connect(proxy.port, f"CONNECT 127.0.0.1:{closed} HTTP/1.1\r\n\r\n".encode()) as s:
                self.assertTrue(read_head(s).startswith(b"HTTP/1.1 502"))
            with connect(proxy.port, b"CONNECT no-port HTTP/1.1\r\n\r\n") as s:
                self.assertTrue(read_head(s).startswith(b"HTTP/1.1 400"))

    def test_binds_loopback_only(self):
        with self.assertRaises(SystemExit):
            loopback_proxy.check_listen("0.0.0.0")
        loopback_proxy.check_listen("127.0.0.1")
        loopback_proxy.check_listen("::1")

    def test_authority_parsing(self):
        self.assertEqual(loopback_proxy.parse_authority(b"api.example.com:443"), ("api.example.com", 443))
        self.assertEqual(loopback_proxy.parse_authority(b"[::1]:8443"), ("::1", 8443))
        for bad in (b"api.example.com", b"host:0", b"host:99999", b":443", b"a:b:443", b"[::1]443"):
            self.assertIsNone(loopback_proxy.parse_authority(bad), bad)


class Probe(unittest.TestCase):
    def test_free_port_is_absent(self):
        state, _ = loopback_proxy.probe("127.0.0.1", free_port(), timeout=2)
        self.assertEqual(state, "absent")

    def test_bundled_proxy_is_a_proxy(self):
        with BundledProxy() as proxy:
            state, detail = loopback_proxy.probe("127.0.0.1", proxy.port, timeout=5)
        self.assertEqual(state, "proxy")
        self.assertIn("tunnel verified", detail)

    def test_http_server_is_other(self):
        web = http_server()
        try:
            state, detail = loopback_proxy.probe("127.0.0.1", web.server_address[1], timeout=5)
        finally:
            stop(web)
        self.assertEqual(state, "other")
        self.assertIn("answered CONNECT", detail)

    def test_silent_service_is_other(self):
        quiet = socket.socket()
        quiet.bind(("127.0.0.1", 0))
        quiet.listen(1)
        try:
            state, detail = loopback_proxy.probe("127.0.0.1", quiet.getsockname()[1], timeout=1)
        finally:
            quiet.close()
        self.assertEqual(state, "other")
        self.assertIn("no HTTP status line", detail)


class EnsureBuilderProxy(unittest.TestCase):
    def test_existing_listener_is_used_as_is(self):
        with mock.patch.object(ed.loopback_proxy, "probe", return_value=("proxy", "HTTP/1.1 200 OK (tunnel verified)")), \
                mock.patch.object(ed.util, "run") as run:
            record = ed.ensure_builder_proxy()
        run.assert_not_called()
        self.assertEqual(record["source"], "pre-existing listener")
        self.assertEqual(record["address"], "127.0.0.1:7890")

    def test_port_held_by_another_service_stops_the_run(self):
        with mock.patch.object(ed.loopback_proxy, "probe", return_value=("other", "127.0.0.1:7890 answered CONNECT "
                                                                                    "with 'HTTP/1.0 501'")), \
                mock.patch.object(ed.util, "run") as run:
            with self.assertRaises(SystemExit) as stop:
                ed.ensure_builder_proxy()
        run.assert_not_called()
        self.assertIn("not an HTTP CONNECT proxy", str(stop.exception))

    def _start(self, load_state):
        probes = iter([("absent", "nothing listens"), ("absent", "nothing listens"), ("proxy", "HTTP/1.1 200 (tunnel verified)")])
        done = types.SimpleNamespace(returncode=0, stdout="Running as unit")
        with mock.patch.object(ed.loopback_proxy, "probe", side_effect=lambda *a, **k: next(probes)), \
                mock.patch.object(ed.util, "out", return_value=load_state), \
                mock.patch.object(ed.util, "run", return_value=done) as run, \
                mock.patch.object(ed.time, "sleep"):
            record = ed.ensure_builder_proxy()
        argv = run.call_args.args[0]
        return record, argv

    def test_free_port_starts_the_bundled_proxy(self):
        record, argv = self._start("not-found")
        self.assertEqual(argv[0], "systemd-run")
        self.assertIn("--unit=agentswe-loopback-proxy", argv)
        self.assertEqual(argv[-3:], [str(Path(loopback_proxy.__file__).resolve()), "--listen", "127.0.0.1:7890"])
        self.assertIn("-I", argv)
        self.assertEqual(record["source"], "bundled")
        self.assertEqual(record["unit"], "agentswe-loopback-proxy.service")

    def test_existing_dead_unit_name_is_not_reused(self):
        record, argv = self._start("loaded")
        unit = next(a for a in argv if a.startswith("--unit="))
        self.assertTrue(unit.startswith("--unit=agentswe-loopback-proxy-"), unit)
        self.assertNotEqual(record["unit"], "agentswe-loopback-proxy.service")

    def test_a_proxy_that_never_comes_up_stops_the_run(self):
        done = types.SimpleNamespace(returncode=0, stdout="")
        with mock.patch.object(ed.loopback_proxy, "probe", return_value=("absent", "nothing listens")), \
                mock.patch.object(ed.util, "out", return_value="not-found"), \
                mock.patch.object(ed.util, "run", return_value=done), mock.patch.object(ed.time, "sleep"):
            with self.assertRaises(SystemExit) as stop:
                ed.ensure_builder_proxy(wait=0)
        self.assertIn("did not serve", str(stop.exception))

    def test_doctor_reports_the_port(self):
        for state, status, text in (("proxy", doctor.OK, "listener present"), ("absent", doctor.OK, "bundled proxy"),
                                    ("other", doctor.FAIL, "not an HTTP CONNECT proxy")):
            with mock.patch.object(loopback_proxy, "probe", return_value=(state, "detail")):
                check = doctor.builder_proxy_check()
            self.assertEqual(check.status, status)
            self.assertIn(text, check.detail)


class FormalBuilderURL(unittest.TestCase):
    """runners/editing/tools/launch_formal_task.py passes the configured Builder URL to one-stops that declare it."""

    def load(self):
        stubs = {"formal_config": types.ModuleType("formal_config"), "formal_commands": types.ModuleType("formal_commands"),
                 "control_runtime": types.ModuleType("control_runtime")}
        stubs["formal_config"].TASKS = {}
        stubs["control_runtime"].control_command = lambda python, script, *a: [str(python), str(script), *a]
        spec = importlib.util.spec_from_file_location("launch_formal_task_under_test",
                                                      ROOT / "runners" / "editing" / "tools" / "launch_formal_task.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        return module

    def test_declared_flag_gets_the_configured_url(self):
        m = self.load()
        help_text = "usage: formal_one_stop.py [--builder-base-url BUILDER_BASE_URL] [--builder-proxy BUILDER_PROXY]"
        env = {"AGENTSWE_BUILDER_BASE_URL": "https://provider.example/v1"}
        self.assertEqual(m.builder_provider_args(help_text, env), ["--builder-base-url", "https://provider.example/v1"])

    def test_undeclared_flag_or_unset_url_adds_nothing(self):
        m = self.load()
        self.assertEqual(m.builder_provider_args("usage: x [--builder-image IMAGE]",
                                                 {"AGENTSWE_BUILDER_BASE_URL": "https://provider.example/v1"}), [])
        self.assertEqual(m.builder_provider_args("usage: x [--builder-base-url URL]", {}), [])
        # an embedded tool's option table mentioning a longer flag is not a declaration
        self.assertEqual(m.builder_provider_args("usage: x [--builder-base-url-file F]",
                                                 {"AGENTSWE_BUILDER_BASE_URL": "https://p/v1"}), [])

    def test_runner_exports_the_url_to_the_launcher(self):
        source = (ROOT / "agentswe" / "runners" / "editing_agentloop_v1.py").read_text()
        self.assertIn('env["AGENTSWE_BUILDER_BASE_URL"] = b.base_url', source)


if __name__ == "__main__":
    unittest.main()
