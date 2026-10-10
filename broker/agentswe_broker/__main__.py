"""Command line for the AgentSWE unified broker.

Key handling: the provider key is read either from ``--credential-file`` (exactly
``--credential-var``; the runner writes a 0600 file and mounts it read-only) or
from the role's ``AGENTSWE_<ROLE>_API_KEY`` / ``AGENTSWE_DEFAULT_API_KEY`` in the
environment or ``--env-file``.  It is never printed: the ready line and
``describe`` report only whether a key is set and which variable supplied it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import chat_translate, config
from .server import LEGACY_PLACEHOLDERS, STATS_TOKEN, BrokerServer, Options

ROLE_DEFAULTS = {
    # Builder: Codex dies on a bare 429 and only retries 5xx (measured 2026-09-22).
    "builder": {"rate_limit_policy": "as_503", "replay_policy": "resend"},
    "runtime": {"rate_limit_policy": "passthrough", "replay_policy": "refuse"},
    "judge": {"rate_limit_policy": "passthrough", "replay_policy": "refuse"},
    "search": {"rate_limit_policy": "passthrough", "replay_policy": "refuse"},
}


def _options(args: argparse.Namespace) -> tuple[Options, str, str | None]:
    env = config.environment(args.env_file)
    role_cfg = config.resolve(args.role, env, require_key=args.credential_file is None)
    if args.credential_file is not None:
        variable = args.credential_var or "AGENTSWE_%s_API_KEY" % args.role.upper()
        key = config.read_credential(args.credential_file, variable)
        key_source = "credential_file:%s" % variable
    else:
        key = role_cfg.api_key or ""
        key_source = role_cfg.api_key_var
    wire = args.wire or role_cfg.wire
    base_url = args.base_url or role_cfg.base_url
    if not base_url:
        raise SystemExit("AGENTSWE_%s_BASE_URL (or AGENTSWE_DEFAULT_BASE_URL) is not set" % args.role.upper())
    provider_url = config.endpoint_for(base_url, wire)
    defaults = ROLE_DEFAULTS[args.role]
    tokens = tuple(t for t in (args.client_token or []) if t) or LEGACY_PLACEHOLDERS
    effort = config.normalize_effort(args.effort) if args.effort is not None else role_cfg.effort
    options = Options(
        role=args.role, upstream_wire=wire, provider_url=provider_url,
        model=(args.model if args.model is not None else role_cfg.model) or None,
        effort=effort,
        effort_policy=args.effort_policy, model_policy=args.model_policy,
        reasoning_mode=args.reasoning_mode, reasoning_summary=args.reasoning_summary,
        default_max_tokens=args.default_max_tokens,
        degenerate_response_policy=args.degenerate_response_policy,
        rate_limit_policy=args.rate_limit_policy or defaults["rate_limit_policy"],
        retry_after_seconds=args.retry_after_seconds,
        upstream_error_mode=args.upstream_error_mode,
        replay_policy=args.replay_policy or defaults["replay_policy"],
        max_attempts=args.max_attempts,
        idle_timeout_seconds=args.idle_timeout_seconds, max_stream_seconds=args.max_stream_seconds,
        max_call_seconds=args.max_call_seconds, keepalive_seconds=args.keepalive_seconds,
        nonstream_keepalive=not args.no_nonstream_keepalive,
        normalize_reported_model=args.normalize_reported_model,
        client_tokens=tokens, stats_token=args.stats_token,
        stats_file=args.stats_file, ledger_dir=args.ledger_dir, dump_bodies=args.dump_bodies,
        user_agent=args.user_agent)
    if args.default_max_tokens < 0:
        raise SystemExit("--default-max-tokens must not be negative")
    return options, key, key_source


def _serve(args: argparse.Namespace) -> int:
    options, key, key_source = _options(args)
    wire, provider_url = options.upstream_wire, options.provider_url
    server = BrokerServer((args.bind, args.port), options, key)
    print(json.dumps({"ready": True, "role": options.role, "model": options.model,
                      "reasoning_effort": options.effort, "upstream_wire": wire,
                      "upstream_host": provider_url.split("/")[2], "key_source": key_source,
                      "bind": args.bind, "port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def _probe(args: argparse.Namespace) -> int:
    from .probe import run_probe
    options, key, _ = _options(args)
    report = run_probe(options, key, image=args.image, timeout=args.probe_timeout)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def _describe(args: argparse.Namespace) -> int:
    env = config.environment(args.env_file)
    print(json.dumps(config.describe_all(env), indent=2))
    return 0


def _credential_file(args: argparse.Namespace) -> int:
    env = config.environment(args.env_file)
    cfg = config.resolve(args.role, env)
    variable = "AGENTSWE_%s_API_KEY" % args.role.upper()
    config.write_credential_file(args.out, variable, cfg.api_key or "")
    print(json.dumps({"written": str(args.out), "variable": variable, "mode": "0600"}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentswe_broker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run one role's broker")
    probe = sub.add_parser("probe", help="send 2-3 tiny requests through this role's broker and report capabilities")
    serve.add_argument("--role", choices=config.ROLES, required=True)
    serve.add_argument("--env-file", type=Path)
    serve.add_argument("--credential-file", type=Path)
    serve.add_argument("--credential-var")
    serve.add_argument("--wire", choices=config.WIRES + config.SEARCH_WIRES,
                       help="override AGENTSWE_<ROLE>_WIRE")
    serve.add_argument("--base-url", help="override AGENTSWE_<ROLE>_BASE_URL")
    serve.add_argument("--model", help="pinned upstream model; empty string = do not pin")
    serve.add_argument("--effort", help="pinned effort spelling; 'none' = send no effort")
    serve.add_argument("--effort-policy", choices=("lock", "passthrough"), default="lock")
    serve.add_argument("--model-policy", choices=("lock", "passthrough"), default="lock")
    serve.add_argument("--reasoning-mode", choices=("carry", "drop"), default="carry")
    serve.add_argument("--reasoning-summary", choices=("emit", "drop"), default="emit")
    serve.add_argument("--default-max-tokens", type=int,
                       default=int(os.environ.get(chat_translate.DEFAULT_MAX_TOKENS_ENV, chat_translate.DEFAULT_MAX_TOKENS)))
    serve.add_argument("--degenerate-response-policy", choices=("off", "end-token", "any"), default="end-token")
    serve.add_argument("--rate-limit-policy", choices=("passthrough", "retry_after", "as_503"))
    serve.add_argument("--retry-after-seconds", type=float, default=5.0)
    serve.add_argument("--upstream-error-mode", choices=("http", "sse"), default="http")
    serve.add_argument("--replay-policy", choices=("refuse", "resend"))
    serve.add_argument("--max-attempts", type=int, default=8)
    serve.add_argument("--idle-timeout-seconds", type=float, default=900.0)
    serve.add_argument("--max-stream-seconds", type=float, default=3600.0)
    serve.add_argument("--max-call-seconds", type=float, default=3600.0)
    serve.add_argument("--keepalive-seconds", type=float, default=5.0)
    serve.add_argument("--no-nonstream-keepalive", action="store_true")
    serve.add_argument("--normalize-reported-model", action="store_true")
    serve.add_argument("--client-token", action="append",
                       help="accepted client placeholder (repeatable); default: the legacy placeholders")
    serve.add_argument("--stats-token", default=STATS_TOKEN)
    serve.add_argument("--stats-file", type=Path)
    serve.add_argument("--ledger-dir", type=Path)
    serve.add_argument("--dump-bodies", action="store_true")
    serve.add_argument("--user-agent", default="agentswe-broker/1.0")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=_serve)
    for action in serve._actions:  # probe accepts every serve option
        if action.dest in ("help",):
            continue
        probe._add_action(action)
    probe.add_argument("--image", action="store_true", help="also check image input (vision tasks)")
    probe.add_argument("--probe-timeout", type=float, default=300.0)
    probe.set_defaults(func=_probe)

    describe = sub.add_parser("describe", help="print the resolved non-secret configuration")
    describe.add_argument("--env-file", type=Path)
    describe.set_defaults(func=_describe)

    cred = sub.add_parser("credential-file", help="write one role's key to a fresh 0600 file")
    cred.add_argument("--role", choices=config.ROLES, required=True)
    cred.add_argument("--out", type=Path, required=True)
    cred.add_argument("--env-file", type=Path)
    cred.set_defaults(func=_credential_file)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
