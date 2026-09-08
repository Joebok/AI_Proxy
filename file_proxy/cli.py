from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from dataclasses import replace

from .engine import Proxy, ProxyAlreadyRunning
from .http_proxy import (
    COMFYUI_PROFILE,
    DEFAULT_BYPASS_ROUTES,
    HttpProxyConfig,
    OLLAMA_PROFILE,
    parse_bypass_route,
    run_http_proxies,
)
from .registry import Registry
from .runtime import RuntimeConfig


def _add_run_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--poll-seconds", type=float, default=1.0)
    command.add_argument("--sync-grace-seconds", type=float, default=300.0)
    command.add_argument("--max-resource-streak", type=int)
    command.add_argument("--forge-upstream", default="http://127.0.0.1:7860")
    command.add_argument("--forge-unload-timeout-seconds", type=float, default=30.0)
    command.add_argument("--require-forge-cleanup", action="store_true")
    command.add_argument("--http-listen-host", default="127.0.0.1")
    command.add_argument("--http-listen-port", type=int)
    command.add_argument("--ollama-upstream", default="http://127.0.0.1:11434")
    command.add_argument("--http-continuation-grace-seconds", type=float, default=2.0)
    command.add_argument("--http-max-body-bytes", type=int)
    command.add_argument("--http-upstream-timeout-seconds", type=float)
    command.add_argument("--http-max-admitted-requests", type=int)
    command.add_argument("--http-max-buffered-body-bytes", type=int)
    command.add_argument("--http-queue-wait-seconds", type=float)
    command.add_argument("--http-bypass-route", action="append", default=[], metavar="'METHOD /path'")
    command.add_argument("--comfyui-listen-port", type=int, default=None)
    command.add_argument("--comfyui-upstream")
    command.add_argument("--http-settle-poll-seconds", type=float, default=1.0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="file-proxy")
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--registry-dir", required=True, type=Path)
    result.add_argument("--runtime-config", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    _add_run_arguments(commands.add_parser("run", help="run without an interactive UI"))
    _add_run_arguments(commands.add_parser("tui", help="run with the terminal dashboard"))
    commands.add_parser("once")
    status = commands.add_parser("status")
    status.add_argument("--subscriber")
    status.add_argument("--job")
    validate = commands.add_parser("validate-subscriber")
    validate.add_argument("subscriber_id")
    return result


def _http_configs(
    args: argparse.Namespace, runtime: RuntimeConfig, comfyui_upstream: str
) -> list[HttpProxyConfig]:
    configs: list[HttpProxyConfig] = []
    if args.http_listen_port is not None:
        bypass_routes = set(DEFAULT_BYPASS_ROUTES)
        bypass_routes.update(parse_bypass_route(value) for value in args.http_bypass_route)
        configs.append(
            HttpProxyConfig(
                listen_host=args.http_listen_host,
                listen_port=args.http_listen_port,
                upstream=args.ollama_upstream,
                continuation_grace_seconds=args.http_continuation_grace_seconds,
                max_body_bytes=args.http_max_body_bytes if args.http_max_body_bytes is not None else 100 * 1024 * 1024,
                upstream_timeout_seconds=runtime.generation_settlement_seconds,
                queue_wait_seconds=runtime.queue_wait_seconds,
                max_admitted_requests=runtime.max_admitted_requests,
                max_buffered_body_bytes=runtime.max_buffered_body_bytes,
                cancellation_grace_seconds=runtime.cancellation_grace_seconds,
                bypass_routes=frozenset(bypass_routes),
                profile=OLLAMA_PROFILE,
                settle_poll_seconds=args.http_settle_poll_seconds,
            )
        )
    if args.comfyui_listen_port is not None:
        configs.append(
            HttpProxyConfig(
                listen_host=args.http_listen_host,
                listen_port=args.comfyui_listen_port,
                upstream=comfyui_upstream,
                continuation_grace_seconds=args.http_continuation_grace_seconds,
                max_body_bytes=args.http_max_body_bytes if args.http_max_body_bytes is not None else 100 * 1024 * 1024,
                upstream_timeout_seconds=runtime.generation_settlement_seconds,
                queue_wait_seconds=runtime.queue_wait_seconds,
                max_admitted_requests=runtime.max_admitted_requests,
                max_buffered_body_bytes=runtime.max_buffered_body_bytes,
                cancellation_grace_seconds=runtime.cancellation_grace_seconds,
                profile=COMFYUI_PROFILE,
                settle_poll_seconds=args.http_settle_poll_seconds,
            )
        )
    return configs


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        runtime_path = args.runtime_config.resolve() if args.runtime_config else None
        runtime = RuntimeConfig.load(runtime_path).resolved(runtime_path.parent if runtime_path else Path.cwd())
        cli_resource_streak = getattr(args, "max_resource_streak", None)
        cli_settlement = getattr(args, "http_upstream_timeout_seconds", None)
        cli_admitted = getattr(args, "http_max_admitted_requests", None)
        cli_buffered = getattr(args, "http_max_buffered_body_bytes", None)
        cli_queue_wait = getattr(args, "http_queue_wait_seconds", None)
        runtime = replace(
            runtime,
            max_resource_streak=cli_resource_streak if cli_resource_streak is not None else runtime.max_resource_streak,
            generation_settlement_seconds=cli_settlement if cli_settlement is not None else runtime.generation_settlement_seconds,
            max_admitted_requests=cli_admitted if cli_admitted is not None else runtime.max_admitted_requests,
            max_buffered_body_bytes=cli_buffered if cli_buffered is not None else runtime.max_buffered_body_bytes,
            queue_wait_seconds=cli_queue_wait if cli_queue_wait is not None else runtime.queue_wait_seconds,
        )
        cli_comfyui_upstream = getattr(args, "comfyui_upstream", None)
        comfyui_upstream = cli_comfyui_upstream if cli_comfyui_upstream is not None else runtime.managed_comfyui.upstream
        runtime = replace(runtime, managed_comfyui=replace(runtime.managed_comfyui, upstream=comfyui_upstream))
        runtime.validate()
        registry = Registry.load(args.registry_dir)
        logger = (lambda message: print(f"[file-proxy] {message}", flush=True)) if args.command == "run" else None
        http_logger = (lambda message: print(f"[http-proxy] {message}", flush=True)) if args.command == "run" else None
        proxy = Proxy(
            args.root,
            registry,
            logger=logger,
            sync_grace_seconds=getattr(args, "sync_grace_seconds", 300.0),
            max_resource_streak=runtime.max_resource_streak,
            forge_upstream=getattr(args, "forge_upstream", None),
            forge_unload_timeout_seconds=getattr(args, "forge_unload_timeout_seconds", 30.0),
            local_runtime_dir=Path(runtime.runtime_dir),
            forge_cleanup_required=getattr(args, "require_forge_cleanup", False),
        )
        if args.command in {"run", "tui"}:
            configs = _http_configs(args, runtime, comfyui_upstream)
        if args.command == "run":
            try:
                if not configs:
                    proxy.run(args.poll_seconds)
                else:
                    asyncio.run(
                        run_http_proxies(proxy, configs, args.poll_seconds, http_logger, runtime)
                    )
            except KeyboardInterrupt:
                print("\n[file-proxy] Stopped.", flush=True)
                return 0
        elif args.command == "tui":
            from .supervisor import RuntimeSupervisor
            from .tui import ProxyDashboard

            supervisor = RuntimeSupervisor(
                proxy,
                configs,
                runtime,
                poll_seconds=args.poll_seconds,
            )
            ProxyDashboard(supervisor).run()
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
