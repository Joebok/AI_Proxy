from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from .scheduler import HttpQueue

if TYPE_CHECKING:
    from collections.abc import Callable

    from .engine import Proxy


DEFAULT_BYPASS_ROUTES = frozenset(
    {
        ("GET", "/api/ps"),
        ("GET", "/api/tags"),
        ("GET", "/api/version"),
        ("GET", "/v1/models"),
    }
)
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
MODEL_ROUTES = frozenset(
    {
        ("POST", "/api/chat"),
        ("POST", "/api/embed"),
        ("POST", "/api/embeddings"),
        ("POST", "/api/generate"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/completions"),
        ("POST", "/v1/embeddings"),
    }
)


@dataclass(frozen=True)
class HttpProxyConfig:
    listen_host: str
    listen_port: int
    upstream: str
    continuation_grace_seconds: float = 2.0
    max_body_bytes: int = 100 * 1024 * 1024
    upstream_timeout_seconds: float = 7500.0
    bypass_routes: frozenset[tuple[str, str]] = field(default_factory=lambda: DEFAULT_BYPASS_ROUTES)

    def validate(self) -> None:
        parsed = urlsplit(self.upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("ollama upstream must be an http or https URL")
        if parsed.query or parsed.fragment:
            raise ValueError("ollama upstream cannot contain a query or fragment")
        if not 1 <= self.listen_port <= 65535:
            raise ValueError("HTTP listen port must be between 1 and 65535")
        if self.continuation_grace_seconds < 0:
            raise ValueError("HTTP continuation grace seconds cannot be negative")
        if self.max_body_bytes <= 0:
            raise ValueError("HTTP maximum body bytes must be positive")
        if self.upstream_timeout_seconds <= 0:
            raise ValueError("HTTP upstream timeout seconds must be positive")
        upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if upstream_port == self.listen_port and _same_endpoint(self.listen_host, parsed.hostname):
            raise ValueError("HTTP listener and Ollama upstream cannot use the same address and port")


def parse_bypass_route(value: str) -> tuple[str, str]:
    try:
        method, path = value.split(maxsplit=1)
    except ValueError as exc:
        raise ValueError("HTTP bypass route must be formatted as 'METHOD /path'") from exc
    method = method.upper()
    if not method.isalpha() or not path.startswith("/"):
        raise ValueError("HTTP bypass route must be formatted as 'METHOD /path'")
    return method, path


def _same_endpoint(left: str, right: str) -> bool:
    if left in {"0.0.0.0", "::"}:
        return True
    try:
        left_addresses = {
            item[4][0]
            for item in socket.getaddrinfo(left, None, type=socket.SOCK_STREAM)
        }
        right_addresses = {
            item[4][0]
            for item in socket.getaddrinfo(right, None, type=socket.SOCK_STREAM)
        }
    except OSError:
        return left.casefold() == right.casefold()
    return bool(left_addresses & right_addresses)


def _forward_headers(headers: CIMultiDict[str], *, request: bool) -> list[tuple[str, str]]:
    blocked = set(HOP_BY_HOP_HEADERS)
    for connection in headers.getall("Connection", []):
        blocked.update(value.strip().casefold() for value in connection.split(","))
    if request:
        blocked.add("host")
    return [(name, value) for name, value in headers.items() if name.casefold() not in blocked]


def request_resource_key(method: str, path: str, body: bytes) -> str | None:
    if (method, path) not in MODEL_ROUTES:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    model = str(payload.get("model") or "").strip() if isinstance(payload, dict) else ""
    return f"ollama:{model}" if model else None


class HttpProxyService:
    def __init__(
        self,
        config: HttpProxyConfig,
        queue: HttpQueue,
        logger: Callable[[str], None],
    ) -> None:
        config.validate()
        self.config = config
        self.queue = queue
        self.logger = logger
        self._upstream = config.upstream.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.config.upstream_timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout, auto_decompress=False)
        app = web.Application(client_max_size=self.config.max_body_bytes)
        app.router.add_route("*", "/{path:.*}", self.handle)
        self._runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.config.listen_host, self.config.listen_port)
            await site.start()
        except BaseException:
            await self._runner.cleanup()
            await self._session.close()
            self._runner = None
            self._session = None
            raise
        self.logger(
            f"HTTP proxy listening on http://{self.config.listen_host}:{self.config.listen_port}; "
            f"upstream {self._upstream}"
        )

    async def stop(self) -> None:
        self.queue.close()
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        route = (request.method, request.path)
        if route in self.config.bypass_routes:
            return await self._relay(request, body, None)

        try:
            job = self.queue.enqueue(request_resource_key(request.method, request.path, body))
        except RuntimeError:
            raise web.HTTPServiceUnavailable(text="AI Proxy is shutting down") from None
        resource_tag = f"[{job.resource_key}] " if job.resource_key else ""
        self.logger(f"Queued HTTP request {job.request_id}: {request.method} {request.path}")
        try:
            await job.started
            if job.cancelled or self.queue.closed:
                raise web.HTTPServiceUnavailable(text="AI Proxy is shutting down")
            self.logger(f"{resource_tag}Starting HTTP request {job.request_id}: {request.method} {request.path}")
            return await self._relay(request, body, job.request_id)
        except asyncio.CancelledError:
            self.queue.cancel(job)
            raise
        finally:
            if job.started.done():
                self.queue.finish(job)
            else:
                self.queue.cancel(job)

    async def _relay(
        self,
        request: web.Request,
        body: bytes,
        request_id: str | None,
    ) -> web.StreamResponse:
        assert self._session is not None
        url = f"{self._upstream}{request.raw_path}"
        downstream: web.StreamResponse | None = None
        try:
            async with self._session.request(
                request.method,
                url,
                headers=_forward_headers(request.headers, request=True),
                data=body,
                allow_redirects=False,
            ) as upstream:
                response_headers = CIMultiDict(_forward_headers(upstream.headers, request=False))
                if request_id is not None:
                    response_headers["X-AI-Proxy-Request-ID"] = request_id
                downstream = web.StreamResponse(status=upstream.status, headers=response_headers)
                await downstream.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await downstream.write(chunk)
                await downstream.write_eof()
                if request_id is not None:
                    self.logger(f"Finished HTTP request {request_id}: {upstream.status}")
                return downstream
        except asyncio.TimeoutError:
            if downstream is None:
                raise web.HTTPGatewayTimeout(text="Ollama upstream timed out") from None
            downstream.force_close()
            return downstream
        except (aiohttp.ClientError, ConnectionError):
            if downstream is None:
                raise web.HTTPBadGateway(text="Ollama upstream request failed") from None
            downstream.force_close()
            return downstream


async def run_http_proxy(
    proxy: Proxy,
    config: HttpProxyConfig,
    poll_seconds: float,
    http_logger: Callable[[str], None] | None = None,
) -> None:
    queue = HttpQueue()
    logger = http_logger or proxy.logger
    service = HttpProxyService(config, queue, logger)
    proxy.ensure_layout()
    proxy.logger(f"Queue root: {proxy.root}")
    with proxy.lock():
        proxy.logger(f"Lock acquired: {proxy.control_root / 'proxy.lock'}")
        recovered = proxy.recover_interrupted()
        if recovered:
            proxy.logger(f"Recovered {recovered} interrupted job(s)")
        await service.start()
        logger("Ready; HTTP requests have priority over filesystem jobs. Press Ctrl-C to stop.")
        try:
            await proxy.run_scheduler(
                queue,
                poll_seconds=poll_seconds,
                continuation_grace_seconds=config.continuation_grace_seconds,
            )
        finally:
            await service.stop()
