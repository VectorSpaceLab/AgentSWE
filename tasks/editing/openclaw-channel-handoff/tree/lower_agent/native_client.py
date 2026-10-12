#!/usr/bin/env python3
"""Thin native case RPC client. No workflow, recovery policy or answer writer."""
from __future__ import annotations
import argparse
import http.client
import json
from pathlib import Path
import socket
import sys


class Connection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=600)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/agentswe/case.sock')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('context','rpc','batch'))
    parser.add_argument('--json', help='Explicit request JSON; otherwise read JSON from stdin')
    args = parser.parse_args(argv)
    c = Connection()
    try:
        if args.action=='context':
            c.request('GET','/native-case/context')
        else:
            value = json.loads(args.json if args.json is not None else sys.stdin.read())
            c.request('POST','/native-case/'+args.action,body=json.dumps(value).encode(),
                      headers={'Content-Type':'application/json'})
        response = c.getresponse()
        raw = response.read()
        try:
            value = json.loads(raw)
        except ValueError:
            value = {'error':'non_json_client_response','http_status':response.status}
        print(json.dumps(value,ensure_ascii=False))
        return 0 if 200<=response.status<300 else 1
    except (OSError,ValueError,http.client.HTTPException) as exc:
        print(json.dumps({'error':'case_client_interrupted','type':type(exc).__name__,
                          'outcome':'unknown; inspect context and native state before deciding whether to retry'}))
        return 1
    finally:
        c.close()


if __name__=='__main__':
    raise SystemExit(main())
