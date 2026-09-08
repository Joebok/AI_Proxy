from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import socket
from typing import TYPE_CHECKING
import uuid
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from .scheduler import HttpQueue

if TYPE_CHECKING:
    from collections.abc import Callable

    from .engine import Proxy
    from .scheduler import HttpJob


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

# ComfyUI routes that hold the GPU slot. These are the only routes the ComfyUI
# profile queues; every other route bypasses the queue. Destructive control
# routes (POST /queue, POST /history) are listed deliberately so they cannot
# erase evidence a tracked prompt is still using.
COMFYUI_QUEUE_ROUTES = frozenset(
    {
        ("POST", "/prompt"),
        ("POST", "/api/prompt"),
        ("POST", "/sdapi/v1/txt2img"),
        ("POST", "/sdapi/v1/img2img"),
        ("POST", "/sdapi/v1/extrapolate"),
        ("POST", "/sdapi/v1/upscale"),
        ("POST", "/queue"),
        ("POST", "/api/queue"),
        ("POST", "/history"),
        ("POST", "/api/history"),
    }
)
# ComfyUI routes that hold the slot until a parsed history entry exists for the
# submitted prompt, rather than until the (tiny) response closes.
COMFYUI_SETTLE_ROUTES = frozenset(
    {
        ("POST", "/prompt"),
        ("POST", "/api/prompt"),
    }
)


def _is_valid_prompt_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and value.strip() != ""


def _workflow_checkpoint(body: bytes) -> str | None:
    """First non-empty ``ckpt_name`` across the workflow's node input dicts."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    workflow = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(workflow, dict):
        return None
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        value = inputs.get("ckpt_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def request_resource_key(method: str, path: str, body: bytes) -> str | None:
    if (method, path) not in MODEL_ROUTES:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    model = str(payload.get("model") or "").strip() if isinstance(payload, dict) else ""
    return f"ollama:{model}" if model else None


def comfyui_resource_key(method: str, path: str, body: bytes) -> str | None:
    if (method, path) not in COMFYUI_QUEUE_ROUTES:
        return None
    model = _workflow_checkpoint(body)
    return f"comfyui:{model}" if model else None


def _is_valid_upstream_prompt_id(value: object) -> bool:
    """A valid *returned* prompt_id: a local opaque string, never a URL."""
    if not _is_valid_prompt_id(value):
        return False
    split = urlsplit(value)
    if split.scheme not in {"", "http", "https"} or split.hostname:
        return False
    return True


@dataclass(frozen=True)
class BackendProfile:
    name: str
    queue_all_except_bypass: bool
    queue_routes: frozenset[tuple[str, str]]
    settle_routes: frozenset[tuple[str, str]]
    resource_key_fn: Callable[[str, str, bytes], str | None]


OLLAMA_PROFILE = BackendProfile(
    name="ollama",
    queue_all_except_bypass=True,
    queue_routes=frozenset(),
    settle_routes=frozenset(),
    resource_key_fn=request_resource_key,
)
COMFYUI_PROFILE = BackendProfile(
    name="comfyui",
    queue_all_except_bypass=False,
    queue_routes=COMFYUI_QUEUE_ROUTES,
    settle_routes=COMFYUI_SETTLE_ROUTES,
    resource_key_fn=comfyui_resource_key,
)

_PROFILE_NAMES = {OLLAMA_PROFILE.name, COMFYUI_PROFILE.name}


@dataclass(frozen=True)
class HttpProxyConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    upstream: str = "http://127.0.0.1:11434"
    continuation_grace_seconds: float = 2.0
    max_body_bytes: int = 100 * 1024 * 1024
    upstream_timeout_seconds: float = 7500.0
    bypass_routes: frozenset[tuple[str, str]] = field(default_factory=lambda: DEFAULT_BYPASS_ROUTES)
    profile: BackendProfile = OLLAMA_PROFILE
    settle_poll_seconds: float = 1.0

    def validate(self) -> None:
        if not 1 <= self.listen_port <= 65535:
            raise ValueError("HTTP listen port must be between 1 and 65535")
        parsed = urlsplit(self.upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("upstream must be an http or https URL")
        if parsed.query or parsed.fragment:
            raise ValueError("upstream cannot contain a query or fragment")
        if self.continuation_grace_seconds < 0:
            raise ValueError("HTTP continuation grace seconds cannot be negative")
        if self.max_body_bytes <= 0:
            raise ValueError("HTTP maximum body bytes must be positive")
        if self.upstream_timeout_seconds <= 0:
            raise ValueError("HTTP upstream timeout seconds must be positive")
        if self.settle_poll_seconds <= 0:
            raise ValueError("HTTP settle poll seconds must be positive")
        if self.profile.name not in _PROFILE_NAMES:
            raise ValueError(f"unknown backend profile {self.profile.name!r}")
        upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if upstream_port == self.listen_port and _same_endpoint(self.listen_host, parsed.hostname):
            raise ValueError("HTTP listener and upstream cannot use the same address and port")


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


def _prepare_prompt_id(body: bytes, method: str, path: str) -> tuple[bytes, str | None, bool]:
    """Ensure a settle-prompt body carries a known prompt_id before it leaves the proxy.

    Returns ``(forwarded_body, known_prompt_id, body_changed)``. A body that is
    not a ``{"prompt": workflow-object}`` payload is forwarded unchanged with no
    known id (known is ``None``). A valid caller-supplied prompt_id is preserved.
    Otherwise a fresh uuid4 is generated and injected, and ``body_changed`` marks
    that the body (and therefore Content-Length) must be recalculated.
    """
    if not isinstance(body, (bytes, bytearray)):
        return bytes(body), None, False
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return bytes(body), None, False
    if not isinstance(payload, dict) or not _is_valid_upstream_prompt_id(payload.get("prompt")):
        return bytes(body), None, False
    supplied = payload.get("prompt_id")
    if _is_valid_upstream_prompt_id(supplied):
        return bytes(body), supplied, False
    payload["prompt_id"] = uuid.uuid4().hex
    try:
        return json.dumps(payload, separators=(",", ":")).encode(), payload["prompt_id"], True
    except (TypeError, ValueError, UnicodeEncodeError):
        return bytes(body), None, False


def _history_path(path: str) -> str:
    """History poll path matching the prompt route's alias (native or /api)."""
    return "/api/history" if path == "/api/prompt" else "/history"


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
        self.profile = config.profile
        self._upstream = config.upstream.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._lifecycle_tasks: set[asyncio.Task[None]] = set()

    def _route_is_queued(self, route: tuple[str, str]) -> bool:
        if self.profile.queue_all_except_bypass:
            return route not in self.config.bypass_routes
        return route in self.profile.queue_routes

    def _route_is_settle(self, route: tuple[str, str]) -> bool:
        return route in self.profile.settle_routes

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
            f"{self.profile.name} HTTP proxy listening on "
            f"http://{self.config.listen_host}:{self.config.listen_port}; "
            f"upstream {self._upstream}"
        )

    async def stop(self) -> None:
        self.queue.close()
        tasks = list(self._lifecycle_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._lifecycle_tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        route = (request.method, request.path)
        if not self._route_is_queued(route):
            return await self._relay(request, body, None)

        try:
            job = self.queue.enqueue(
                self.profile.resource_key_fn(request.method, request.path, body)
            )
        except RuntimeError:
            raise web.HTTPServiceUnavailable(text="AI Proxy is shutting down") from None
        self.logger(f"Queued {self.profile.name} HTTP request {job.request_id}: {request.method} {request.path}")
        if self._route_is_settle(route):
            # Settle routes transfer ownership to a tracked lifecycle task
            # before any upstream dispatch, so the slot is held until the
            # generation settles even after the response (and the handler)
            # completes; handler cancellation (client disconnect) cannot
            # release the slot early.
            response = asyncio.get_running_loop().create_future()
            lifecycle = asyncio.create_task(
                self._run_prompt_lifecycle(request, job, response, body),
                name=f"prompt-lifecycle-{job.request_id}",
            )
            self._lifecycle_tasks.add(lifecycle)
            lifecycle.add_done_callback(self._lifecycle_tasks.discard)
            try:
                return await response
            except asyncio.CancelledError:
                # The lifecycle keeps running and owns job.finished.
                raise
        resource_tag = f"[{job.resource_key}] " if job.resource_key else ""
        try:
            await job.started
            if job.cancelled or self.queue.closed:
                raise web.HTTPServiceUnavailable(text="AI Proxy is shutting down")
            self.logger(
                f"{resource_tag}Starting {self.profile.name} HTTP request {job.request_id}: "
                f"{request.method} {request.path}"
            )
            return await self._relay(request, body, job.request_id)
        except asyncio.CancelledError:
            self.queue.cancel(job)
            raise
        finally:
            if job.started.done():
                self.queue.finish(job)
            else:
                self.queue.cancel(job)

    async def _run_prompt_lifecycle(
        self,
        request: web.Request,
        job: HttpJob,
        response: asyncio.Future[web.StreamResponse],
        body: bytes,
    ) -> None:
        """Settle-route lifecycle that holds the slot until the prompt settles.

        The scheduler pops the job and signals ``job.started`` before dispatch,
        and this task's ``finally`` owns ``queue.finish`` in every path, so the
        scheduler is unblocked exactly when the generation has settled (or the
        explicit deadline expired). Cancellation before dispatch therefore
        releases the slot cleanly.
        """
        profile = self.profile
        started = False
        try:
            await job.started
            started = True
            if job.cancelled or self.queue.closed:
                self.logger(
                    f"{profile.name} HTTP request {job.request_id} cancelled before dispatch"
                )
                return
            forwarded_body, known_id, body_changed = _prepare_prompt_id(
                body, request.method, request.path
            )
            headers = _forward_headers(request.headers, request=True)
            if body_changed:
                headers = [
                    (name, value)
                    for name, value in headers
                    if name.casefold() != "content-length"
                ]
            dispatched = False
            downstream: web.StreamResponse | None = None
            try:
                async with self._session.request(
                    request.method,
                    f"{self._upstream}{request.raw_path}",
                    headers=headers,
                    data=forwarded_body,
                    allow_redirects=False,
                ) as upstream:
                    dispatched = True
                    status = upstream.status
                    upstream_body = await upstream.content.read()
                    if 200 <= status < 300:
                        returned_id = _extract_prompt_id(upstream_body)
                        if returned_id is None:
                            known_id = None
                        elif known_id is not None and returned_id != known_id:
                            self.logger(
                                f"{profile.name} upstream returned prompt id {returned_id} "
                                f"different from preassigned {known_id}; tracking the returned id"
                            )
                            known_id = returned_id
                        else:
                            known_id = returned_id
                    header_pairs = _forward_headers(upstream.headers, request=False)
                    header_pairs = [
                        (name, value)
                        for name, value in header_pairs
                        if name.casefold() not in {"content-length", "content-encoding", "transfer-encoding"}
                    ]
                    header_pairs.append(("Content-Length", str(len(upstream_body))))
                    header_pairs.append(("X-AI-Proxy-Request-ID", job.request_id))
                    downstream = web.StreamResponse(
                        status=status, headers=CIMultiDict(header_pairs)
                    )
                    try:
                        await downstream.prepare(request)
                        await downstream.write(upstream_body)
                        await downstream.write_eof()
                        if not response.cancelled():
                            response.set_result(downstream)
                    except BaseException:
                        # The client disconnected mid-delivery; the lifecycle still
                        # settles the prompt, and the slot stays until then.
                        try:
                            downstream.force_close()
                        except Exception:
                            pass
                    self.logger(f"Finished {profile.name} HTTP request {job.request_id}: {status}")
                    if 200 <= status < 300 and known_id:
                        settled = await self._wait_for_prompt_completion(known_id, request.path)
                        if not settled:
                            self.logger(
                                f"{profile.name} prompt {known_id} did not settle "
                                "before the settle deadline"
                            )
            except asyncio.TimeoutError:
                self.logger(
                    f"{profile.name} upstream failed for request {job.request_id}"
                    + ("; reconciling by prompt id" if dispatched and known_id else "")
                )
                if dispatched and known_id:
                    try:
                        await self._wait_for_prompt_completion(known_id, request.path)
                    except asyncio.CancelledError:
                        raise
                if not response.cancelled():
                    response.set_exception(
                        web.HTTPGatewayTimeout("upstream timed out")
                    )
            except (aiohttp.ClientError, ConnectionError, OSError):
                self.logger(
                    f"{profile.name} upstream failed for request {job.request_id}"
                    + ("; reconciling by prompt id" if dispatched and known_id else "")
                )
                if dispatched and known_id:
                    try:
                        await self._wait_for_prompt_completion(known_id, request.path)
                    except asyncio.CancelledError:
                        raise
                if downstream is not None:
                    try:
                        downstream.force_close()
                    except Exception:
                        pass
                else:
                    if not response.cancelled():
                        response.set_exception(
                            web.HTTPBadGateway("upstream request failed")
                        )
        except Exception as exc:
            if not response.cancelled():
                response.set_exception(exc)
        finally:
            if started and not job.finished.done():
                self.queue.finish(job)

    async def _wait_for_prompt_completion(self, prompt_id: str, path: str) -> bool:
        """Poll upstream history until prompt_id has a terminal entry.

        Bounded by a single explicit deadline of ``upstream_timeout_seconds``.
        Returns True once a parsed ``{prompt_id: {...}}`` entry exists; any
        terminal entry (including errors or interruptions) frees the GPU.
        """
        assert self._session is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.upstream_timeout_seconds
        url = f"{self._upstream}{_history_path(path)}/{prompt_id}"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            entry = False
            try:
                async with self._session.get(url) as history:
                    if 200 <= history.status < 300:
                        entry = self._parse_terminal_entry(await history.content.read(), prompt_id)
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError):
                entry = False
            if entry:
                return True
            await asyncio.sleep(min(self.config.settle_poll_seconds, remaining))

    @staticmethod
    def _parse_terminal_entry(history_body: bytes, prompt_id: str) -> bool:
        try:
            data = json.loads(history_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and isinstance(data.get(prompt_id), dict)

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
                    self.logger(f"Finished {self.profile.name} HTTP request {request_id}: {upstream.status}")
                return downstream
        except asyncio.CancelledError:
            if downstream is not None:
                downstream.force_close()
            raise
        except asyncio.TimeoutError:
            if downstream is None:
                raise web.HTTPGatewayTimeout(text="upstream timed out") from None
            downstream.force_close()
            return downstream
        except (aiohttp.ClientError, ConnectionError):
            if downstream is None:
                raise web.HTTPBadGateway(text="upstream request failed") from None
            downstream.force_close()
            return downstream


def _extract_prompt_id(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = payload.get("prompt_id") if isinstance(payload, dict) else None
    return value if _is_valid_upstream_prompt_id(value) else None


async def run_http_proxies(
    proxy: Proxy,
    configs: list[HttpProxyConfig],
    poll_seconds: float,
    http_logger: Callable[[str], None] | None = None,
) -> None:
    if not configs:
        raise ValueError("At least one HTTP listener configuration is required")
    for config in configs:
        config.validate()
    seen: set[tuple[str, int]] = set()
    for config in configs:
        key = (config.listen_host.casefold(), config.listen_port)
        if key in seen:
            raise ValueError(
                f"Duplicate or overlapping HTTP listener {config.listen_host}:{config.listen_port}"
            )
        seen.add(key)

    queue = HttpQueue()
    logger = http_logger or proxy.logger
    services = [HttpProxyService(config, queue, logger) for config in configs]
    started: list[HttpProxyService] = []
    proxy.ensure_layout()
    proxy.logger(f"Queue root: {proxy.root}")
    with proxy.lock():
        proxy.logger(f"Lock acquired: {proxy.control_root / 'proxy.lock'}")
        recovered = proxy.recover_interrupted()
        if recovered:
            proxy.logger(f"Recovered {recovered} interrupted job(s)")
        try:
            for service in services:
                await service.start()
                started.append(service)
        except BaseException:
            for service in reversed(started):
                try:
                    await service.stop()
                except BaseException:
                    pass
            raise
        grace = max(config.continuation_grace_seconds for config in configs)
        try:
            logger(
                "Ready; HTTP requests have priority over filesystem jobs for "
                f"{len(services)} listener(s). Press Ctrl-C to stop."
            )
            await proxy.run_scheduler(
                queue,
                poll_seconds=poll_seconds,
                continuation_grace_seconds=grace,
            )
        finally:
            for service in reversed(started):
                try:
                    await service.stop()
                except BaseException:
                    pass


async def run_http_proxy(
    proxy: Proxy,
    config: HttpProxyConfig,
    poll_seconds: float,
    http_logger: Callable[[str], None] | None = None,
) -> None:
    await run_http_proxies(proxy, [config], poll_seconds, http_logger)
