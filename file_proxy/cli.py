from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .engine import Proxy, ProxyAlreadyRunning
from .http_proxy import DEFAULT_BYPASS_ROUTES, HttpProxyConfig, parse_bypass_route, run_http_proxy
from .registry import Registry


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="file-proxy")
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--registry-dir", required=True, type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--poll-seconds", type=float, default=1.0)
    run.add_argument("--sync-grace-seconds", type=float, default=300.0)
    run.add_argument("--http-listen-host", default="127.0.0.1")
    run.add_argument("--http-listen-port", type=int)
    run.add_argument("--ollama-upstream", default="http://127.0.0.1:11434")
    run.add_argument("--http-continuation-grace-seconds", type=float, default=2.0)
    run.add_argument("--http-max-body-bytes", type=int, default=100 * 1024 * 1024)
    run.add_argument("--http-upstream-timeout-seconds", type=float, default=7500.0)
    run.add_argument("--http-bypass-route", action="append", default=[], metavar="'METHOD /path'")
    commands.add_parser("once")
    status = commands.add_parser("status")
    status.add_argument("--subscriber")
    status.add_argument("--job")
    validate = commands.add_parser("validate-subscriber")
    validate.add_argument("subscriber_id")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        registry = Registry.load(args.registry_dir)
        logger = (lambda message: print(f"[file-proxy] {message}", flush=True)) if args.command == "run" else None
        proxy = Proxy(
            args.root,
            registry,
            logger=logger,
            sync_grace_seconds=getattr(args, "sync_grace_seconds", 300.0),
        )
        if args.command == "run":
            try:
                if args.http_listen_port is None:
                    proxy.run(args.poll_seconds)
                else:
                    bypass_routes = set(DEFAULT_BYPASS_ROUTES)
                    bypass_routes.update(parse_bypass_route(value) for value in args.http_bypass_route)
                    config = HttpProxyConfig(
                        listen_host=args.http_listen_host,
                        listen_port=args.http_listen_port,
                        upstream=args.ollama_upstream,
                        continuation_grace_seconds=args.http_continuation_grace_seconds,
                        max_body_bytes=args.http_max_body_bytes,
                        upstream_timeout_seconds=args.http_upstream_timeout_seconds,
                        bypass_routes=frozenset(bypass_routes),
                    )
                    asyncio.run(run_http_proxy(proxy, config, args.poll_seconds))
            except KeyboardInterrupt:
                print("\n[file-proxy] Stopped.", flush=True)
                return 0
        elif args.command == "once":
            answer = proxy.once()
            print(json.dumps({"processed": str(answer) if answer else None}))
        elif args.command == "status":
            print(json.dumps(proxy.status(args.subscriber, args.job), indent=2))
        else:
            print(json.dumps(registry.validate_subscriber(args.subscriber_id), indent=2))
        return 0
    except (OSError, ValueError, ProxyAlreadyRunning, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
