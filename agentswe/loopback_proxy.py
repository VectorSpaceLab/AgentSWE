#!/usr/bin/env python3
"""A minimal HTTP CONNECT proxy on a loopback address (standard library only), and the probe that finds one.

Every Editing task tree's Builder relay forwards the Builder container's CONNECT requests to an HTTP CONNECT proxy
on 127.0.0.1:7890, which is how the published runs were set up. If nothing listens there, `agentswe run` starts
this proxy as a transient systemd unit (agentswe/runners/editing_agentloop_v1.py ensure_builder_proxy). If a proxy
the host already runs (Clash, mihomo, gost) holds that port, `agentswe run` uses it as is.

The proxy answers CONNECT only, binds only a loopback address and opens each tunnel directly, with no upstream
proxy. It never reads, records or changes the bytes it tunnels; the journal gets one line per tunnel (target and
outcome).

    python3 -I -B loopback_proxy.py [--listen 127.0.0.1:7890]
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
import sys
import threading

DEFAULT_LISTEN = ("127.0.0.1", 7890)
HEAD_LIMIT = 8192  # bytes of request head (request line and headers)
HANDSHAKE_TIMEOUT = 15.0  # seconds to read the head and to connect upstream; tunnels themselves have no timeout
CHUNK = 65536


def parse_authority(raw: bytes) -> tuple[str, int] | None:
    """host:port of a CONNECT request line ([v6]:port accepted); None if malformed."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not rest.startswith(":"):
            return None
        port_text = rest[1:]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep or ":" in host:
            return None
    if not host or not port_text.isdigit() or not 0 < int(port_text) < 65536:
        return None
    return host, int(port_text)


async def _close(*writers: asyncio.StreamWriter) -> None:
    for w in writers:
        w.close()
    for w in writers:
        try:
            await w.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
    try:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii"))
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        await _close(writer)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    """Copy until EOF; True on a clean EOF (half-close passed on), False if either side failed."""
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        return False
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError, RuntimeError):
        return False
    return True


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
    except asyncio.LimitOverrunError:
        await _reply(writer, "431 Request Header Fields Too Large")
        return
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
        await _close(writer)
        return
    parts = head.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) != 3 or parts[0] != b"CONNECT" or parts[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        await _reply(writer, "405 Method Not Allowed")
        return
    target = parse_authority(parts[1])
    if target is None:
        await _reply(writer, "400 Bad Request")
        return
    try:
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(*target), HANDSHAKE_TIMEOUT)
    except (asyncio.TimeoutError, OSError) as exc:
        print(f"CONNECT {target[0]}:{target[1]} from {peer}: 502 ({type(exc).__name__})", flush=True)
        await _reply(writer, "502 Bad Gateway")
        return
    print(f"CONNECT {target[0]}:{target[1]} from {peer}: 200", flush=True)
    try:
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        # Bytes the client sent after the head are already buffered in `reader`; the pipe forwards them first.
        to_target = asyncio.ensure_future(_pipe(reader, up_writer))
        to_client = asyncio.ensure_future(_pipe(up_reader, writer))
        done, pending = await asyncio.wait({to_target, to_client}, return_when=asyncio.FIRST_COMPLETED)
        if not all(t.result() for t in done):  # a failed side ends the tunnel; a clean half-close lets the other run
            for t in pending:
                t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except (ConnectionError, OSError):
        pass
    finally:
        await _close(up_writer, writer)


def check_listen(host: str) -> None:
    if not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"loopback_proxy binds a loopback address only, not {host}")


async def serve(host: str, port: int, ready: threading.Event | None = None, bound: list | None = None) -> None:
    check_listen(host)
    server = await asyncio.start_server(handle, host, port, limit=HEAD_LIMIT, reuse_address=True)
    if bound is not None:
        bound.append(server.sockets[0].getsockname()[1])
    print(f"loopback CONNECT proxy listening on {host}:{server.sockets[0].getsockname()[1]}", flush=True)
    if ready is not None:
        ready.set()
    async with server:
        await server.serve_forever()


def probe(host: str = DEFAULT_LISTEN[0], port: int = DEFAULT_LISTEN[1], timeout: float = 5.0) -> tuple[str, str]:
    """What listens on host:port: ("absent", why), ("proxy", status line) or ("other", what it answered).

    A listener is an HTTP CONNECT proxy when it answers `CONNECT 127.0.0.1:<p>` (p: a throwaway listener opened
    here) with status 200. Some proxies answer 200 before they dial, so the detail also says whether the probe's
    bytes came through the tunnel; only the status decides. Nothing leaves the host."""
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError:
        return "absent", f"nothing listens on {host}:{port}"
    except OSError as exc:
        return "absent", f"{host}:{port}: {exc}"
    nonce = b"agentswe-loopback-probe"
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target.settimeout(timeout)

    def answer():
        try:
            peer, _ = target.accept()
            with peer:
                peer.sendall(nonce)
        except OSError:
            pass

    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    try:
        with conn:
            conn.settimeout(timeout)
            authority = f"127.0.0.1:{target.getsockname()[1]}"
            conn.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
            data = b""
            try:
                while b"\r\n\r\n" not in data and len(data) < HEAD_LIMIT:
                    block = conn.recv(4096)
                    if not block:
                        break
                    data += block
            except OSError:
                pass
            status = data.split(b"\r\n", 1)[0].decode("latin-1").strip()
            words = status.split(" ")
            if len(words) < 2 or words[0] not in ("HTTP/1.0", "HTTP/1.1") or words[1] != "200":
                shown = status[:80] if status else "no HTTP status line"
                return "other", f"{host}:{port} answered CONNECT with {shown!r}"
            rest = data.partition(b"\r\n\r\n")[2]
            try:
                while len(rest) < len(nonce):
                    block = conn.recv(4096)
                    if not block:
                        break
                    rest += block
            except OSError:
                pass
            tunnelled = "tunnel verified" if rest.startswith(nonce) else "tunnel not verified"
            return "proxy", f"{status} ({tunnelled})"
    finally:
        target.close()
        worker.join(timeout=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="minimal loopback HTTP CONNECT proxy (direct egress)")
    ap.add_argument("--listen", default="%s:%d" % DEFAULT_LISTEN, help="loopback host:port (default 127.0.0.1:7890)")
    a = ap.parse_args(argv)
    host, _, port = a.listen.rpartition(":")
    check_listen(host)
    try:
        asyncio.run(serve(host, int(port)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
