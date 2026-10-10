#!/usr/bin/env python3
"""TLS front for the evaluator-owned search broker, inside one Candidate compose project.

The Builder-visible task text (unchanged from the scrubbed release) names https://search.example.com/serp_search_v1.
In a Candidate container that name resolves, through a compose network alias, to this front. The front terminates TLS
with a per-run certificate signed by a per-run CA, which only that Candidate container trusts, and relays the bytes
unchanged to the run's search broker. The broker alone holds the real search key; the Candidate keeps the placeholder
token. Standard library only.
"""
from __future__ import annotations

import argparse
import socket
import ssl
import threading


def pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(raw: socket.socket, context: ssl.SSLContext, upstream: tuple[str, int]) -> None:
    try:
        client = context.wrap_socket(raw, server_side=True)
    except (OSError, ssl.SSLError):
        raw.close()
        return
    try:
        broker = socket.create_connection(upstream, timeout=30)
    except OSError:
        client.close()
        return
    broker.settimeout(None)
    client.settimeout(None)
    threading.Thread(target=pipe, args=(client, broker), daemon=True).start()
    pipe(broker, client)
    for sock in (client, broker):
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--upstream-host", required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server = socket.create_server(("0.0.0.0", args.port))
    print(f"search front listening on :{args.port} -> {args.upstream_host}:{args.upstream_port}", flush=True)
    while True:
        raw, _ = server.accept()
        threading.Thread(target=handle, args=(raw, context, (args.upstream_host, args.upstream_port)),
                         daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
