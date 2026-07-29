from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import Proxy, ProxyAlreadyRunning
from .registry import Registry


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="file-proxy")
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--registry-dir", required=True, type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--poll-seconds", type=float, default=1.0)
    run.add_argument("--sync-grace-seconds", type=float, default=300.0)
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
                proxy.run(args.poll_seconds)
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
    except (ValueError, ProxyAlreadyRunning, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
