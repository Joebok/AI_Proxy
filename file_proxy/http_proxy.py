from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import socket
import zlib
from typing import TYPE_CHECKING
import uuid
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from .scheduler import HttpQueue
from .backend import BackendManager, BackendUnavailable
from .store import ExecutionStore
from .runtime import RuntimeConfig

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
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _workflow_signature(body: bytes) -> str | None:
    """Stable signature from recognized ComfyUI model-loader inputs."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    workflow = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(workflow, dict):
        return None
    recognized = ("ckpt_name", "unet_name", "diffusion_model", "clip_name", "clip_name1", "clip_name2", "vae_name", "lora_name")
    values: list[str] = []
    for node_id, node in sorted(workflow.items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key in recognized:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                values.append(f"{key}={value.strip()}")
    return "|".join(values) or None


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
    signature = _workflow_signature(body)
    return f"comfyui:{signature}" if signature else "comfyui"


def _is_valid_upstream_prompt_id(value: object) -> bool:
    """A valid *returned* prompt_id: a local opaque string, never a URL."""
    return _is_valid_prompt_id(value)


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
    queue_wait_seconds: float = 1800.0
    max_admitted_requests: int = 32
    max_buffered_body_bytes: int = 256 * 1024 * 1024
    cancellation_grace_seconds: float = 30.0

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
        if self.queue_wait_seconds <= 0 or self.max_admitted_requests <= 0 or self.max_buffered_body_bytes <= 0:
            raise ValueError("HTTP admission limits must be positive")
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
    if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), dict):
        return bytes(body), None, False
    supplied = payload.get("prompt_id")
    if supplied is not None:
        if _is_valid_upstream_prompt_id(supplied):
            return bytes(body), supplied, False
        raise ValueError("prompt_id must be a canonical UUID")
    payload["prompt_id"] = str(uuid.uuid4())
    try:
        return json.dumps(payload, separators=(",", ":")).encode(), payload["prompt_id"], True
    except (TypeError, ValueError, UnicodeEncodeError):
        return bytes(body), None, False


def _history_path(path: str) -> str:
    """History poll path matching the prompt route's alias (native or /api)."""
    return "/api/history" if path == "/api/prompt" else "/history"


class AdmissionController:
    def __init__(self, max_requests: int, max_bytes: int) -> None:
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.requests = 0
        self.bytes = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            if self.requests >= self.max_requests:
                raise web.HTTPServiceUnavailable(text="AI Proxy admission capacity is full", headers={"Retry-After": "5"})
            self.requests += 1

    async def add(self, amount: int) -> None:
        async with self._lock:
            if self.bytes + amount > self.max_bytes:
                raise web.HTTPServiceUnavailable(text="AI Proxy buffered-body capacity is full", headers={"Retry-After": "5"})
            self.bytes += amount

    async def leave(self, amount: int) -> None:
        async with self._lock:
            self.requests = max(0, self.requests - 1)
            self.bytes = max(0, self.bytes - amount)

    async def release_bytes(self, amount: int) -> None:
        async with self._lock:
            self.bytes = max(0, self.bytes - amount)


@dataclass
class HttpRuntimeState:
    """Live HTTP runtime objects exposed to an in-process dashboard."""

    listener_states: dict[str, str] = field(default_factory=dict)
    queue: HttpQueue | None = None
    services: list["HttpProxyService"] = field(default_factory=list)
    backend_manager: BackendManager | None = None
    store: ExecutionStore | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class HttpProxyService:
    def __init__(
        self,
        config: HttpProxyConfig,
        queue: HttpQueue,
        logger: Callable[[str], None],
        *,
        admission: AdmissionController | None = None,
        store: ExecutionStore | None = None,
        backend_manager: BackendManager | None = None,
        status_provider: Callable[[], dict[str, object]] | None = None,
        retention_days: int = 7,
        cache_max_bytes: int = 10 * 1024 * 1024 * 1024,
    ) -> None:
        config.validate()
        self.config = config
        self.queue = queue
        self.logger = logger
        self.profile = config.profile
        self._upstream = config.upstream.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lifecycle_tasks: set[asyncio.Task[None]] = set()
        self.admission = admission or AdmissionController(config.max_admitted_requests, config.max_buffered_body_bytes)
        self.store = store
        self.backend_manager = backend_manager
        self.status_provider = status_provider
        self.retention_days = retention_days
        self.cache_max_bytes = cache_max_bytes
        self._cache_error: str | None = None

    def _route_is_queued(self, route: tuple[str, str]) -> bool:
        if self.profile.queue_all_except_bypass:
            return route not in self.config.bypass_routes
        if self.backend_manager and self.backend_manager.config.enabled:
            return (
                route not in {("POST", "/interrupt"), ("POST", "/api/interrupt")}
                and route[1] != "/ws"
                and not route[1].startswith("/_proxy/")
            )
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
            self._site = web.TCPSite(
                self._runner, self.config.listen_host, self.config.listen_port
            )
            await self._site.start()
        except BaseException:
            await self._runner.cleanup()
            await self._session.close()
            self._site = None
            self._runner = None
            self._session = None
            raise
        self.logger(
            f"{self.profile.name} HTTP proxy listening on "
            f"http://{self.config.listen_host}:{self.config.listen_port}; "
            f"upstream {self._upstream}"
        )

    async def stop(self) -> None:
        await self.stop_accepting()
        tasks = list(self._lifecycle_tasks)
        if tasks:
            if self.profile.name == "comfyui" and self._session:
                try:
                    async with self._session.post(f"{self._upstream}/interrupt"):
                        pass
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    pass
            _done, pending = await asyncio.wait(tasks, timeout=self.config.cancellation_grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._lifecycle_tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def stop_accepting(self) -> None:
        """Close admission and listening sockets while active work winds down."""

        self.queue.close()
        if self._site is not None:
            await self._site.stop()
            self._site = None

    async def reconcile_records(self) -> None:
        if not self.store:
            return
        for record in self.store.pending():
            prompt_id = record.get("prompt_id")
            if record.get("backend") == "ollama":
                continue
            if record.get("backend") != "comfyui" or not isinstance(prompt_id, str):
                self.store.update(record["request_id"], "failed", outcome="restart", error="ambiguous execution after proxy restart")
                continue
            history = await self._wait_for_prompt_completion(prompt_id, "/prompt", timeout=0.25)
            if history is not None:
                await self._archive_completion(record["request_id"], prompt_id, history)
            elif self.backend_manager and self.backend_manager.config.enabled:
                self.store.update(record["request_id"], "failed", outcome="restart", error="owned backend ended with previous proxy")
            elif self.backend_manager:
                self.backend_manager.block(f"Prompt {prompt_id} was unresolved at restart; reconcile it before GPU execution")
                self.store.update(record["request_id"], "blocked", outcome="unresolved", error=self.backend_manager.blocked_reason)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        route = (request.method, request.path)
        special = await self._special_route(request)
        if special is not None:
            return special
        if not self._route_is_queued(route):
            body = await self._read_body(request, admitted=False)
            return await self._relay(request, body, None)

        buffered = 0
        await self.admission.enter()
        try:
            body = await self._read_body(request, admitted=True)
            buffered = len(body)
        except BaseException:
            await self.admission.leave(0)
            raise

        try:
            job = self.queue.enqueue(
                self.profile.resource_key_fn(request.method, request.path, body),
                backend=self.profile.name,
                barrier=route in {("POST", "/queue"), ("POST", "/api/queue"), ("POST", "/history"), ("POST", "/api/history")},
            )
        except RuntimeError:
            await self.admission.leave(buffered)
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
                self._run_prompt_lifecycle(request, job, response, body, buffered),
                name=f"prompt-lifecycle-{job.request_id}",
            )
            self._lifecycle_tasks.add(lifecycle)
            lifecycle.add_done_callback(self._lifecycle_tasks.discard)
            try:
                return await response
            except asyncio.CancelledError:
                if not job.dispatched:
                    self.queue.cancel(job)
                    lifecycle.cancel()
                raise
        resource_tag = f"[{job.resource_key}] " if job.resource_key else ""
        try:
            try:
                await asyncio.wait_for(asyncio.shield(job.started), self.config.queue_wait_seconds)
            except asyncio.TimeoutError:
                self.queue.cancel(job)
                raise web.HTTPGatewayTimeout(text="AI Proxy queue wait expired") from None
            if job.cancelled or self.queue.closed:
                raise web.HTTPServiceUnavailable(text="AI Proxy is shutting down")
            if job.start_error:
                raise web.HTTPServiceUnavailable(text=job.start_error, headers={"Retry-After": "30"})
            self.logger(
                f"{resource_tag}Starting {self.profile.name} HTTP request {job.request_id}: "
                f"{request.method} {request.path}"
            )
            track_ollama = self.store is not None and self.profile.name == "ollama" and route in MODEL_ROUTES
            if track_ollama:
                self.store.create(job.request_id, self.profile.name, None)
                self.store.update(job.request_id, "dispatched")
            try:
                relayed = await self._relay(request, body, job.request_id)
            except (aiohttp.ClientError, web.HTTPBadGateway, web.HTTPGatewayTimeout) as exc:
                if track_ollama:
                    if self.backend_manager:
                        try:
                            await self.backend_manager.reconcile_ollama(self._upstream)
                        except BackendUnavailable as reconcile_exc:
                            self.store.update(job.request_id, "blocked", outcome="unresolved", error=str(reconcile_exc))
                            self.backend_manager.block("Ollama execution failed ambiguously; unload/reconciliation is required")
                        else:
                            self.store.update(job.request_id, "failed", outcome="reconciled", error=str(exc))
                    else:
                        self.store.update(job.request_id, "blocked", outcome="unresolved", error=str(exc))
                raise
            if track_ollama:
                self.store.update(job.request_id, "completed", outcome="succeeded")
            if self.store and relayed.status < 300 and route in {("POST", "/history"), ("POST", "/api/history")}:
                try:
                    payload = json.loads(body or b"{}")
                    prompt_ids = payload.get("delete") if isinstance(payload, dict) else None
                    if isinstance(prompt_ids, list) and all(isinstance(value, str) for value in prompt_ids):
                        self.store.delete_history(prompt_ids)
                    elif isinstance(payload, dict) and payload.get("clear") is True:
                        self.store.delete_history()
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            return relayed
        except asyncio.CancelledError:
            self.queue.cancel(job)
            raise
        finally:
            if job.started.done():
                self.queue.finish(job)
            else:
                self.queue.cancel(job)
            await self.admission.leave(buffered)

    async def _read_body(self, request: web.Request, *, admitted: bool) -> bytes:
        declared = request.content_length
        if declared is not None and declared > self.config.max_body_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=self.config.max_body_bytes, actual_size=declared)
        chunks: list[bytes] = []
        total = 0
        reserved = 0
        try:
            async for chunk in request.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > self.config.max_body_bytes:
                    raise web.HTTPRequestEntityTooLarge(max_size=self.config.max_body_bytes, actual_size=total)
                if admitted:
                    await self.admission.add(len(chunk))
                    reserved += len(chunk)
                chunks.append(chunk)
        except BaseException:
            if admitted:
                await self.admission.release_bytes(reserved)
            raise
        return b"".join(chunks)

    async def _special_route(self, request: web.Request) -> web.StreamResponse | None:
        if request.method == "GET" and request.path == "/_proxy/status":
            data: dict[str, object] = {"backend": self.profile.name, "queue": self.queue.snapshot()}
            if self.backend_manager:
                data["backend_runtime"] = self.backend_manager.status()
            if self.store:
                data["cache"] = {**self.store.status(), "error": self._cache_error}
            if self.status_provider:
                data["filesystem"] = self.status_provider()
            return web.json_response(data)
        if request.method == "GET" and request.path.startswith("/_proxy/jobs/"):
            record = self.store.get(request.path.rsplit("/", 1)[-1]) if self.store else None
            if not record:
                raise web.HTTPNotFound(text="unknown request id")
            record.pop("history_json", None)
            return web.json_response(record)
        if request.method == "GET" and self.store:
            if request.path in {"/history", "/api/history"} and self.backend_manager and self.backend_manager.config.enabled:
                return web.Response(body=self.store.histories(), content_type="application/json")
            prefix = "/api/history/" if request.path.startswith("/api/history/") else "/history/"
            if request.path.startswith(prefix):
                prompt_id = request.path[len(prefix):]
                cached = self.store.history(prompt_id)
                if cached is not None:
                    return web.Response(body=cached, content_type="application/json")
            if request.path in {"/view", "/api/view"}:
                filename = request.query.get("filename", "")
                subfolder = request.query.get("subfolder", "")
                kind = request.query.get("type", "output")
                prompt_id = request.query.get("prompt_id")
                cached_path = self.store.artifact(prompt_id, filename, subfolder, kind)
                if cached_path:
                    return web.FileResponse(cached_path)
        return None

    async def _run_prompt_lifecycle(
        self,
        request: web.Request,
        job: HttpJob,
        response: asyncio.Future[web.StreamResponse],
        body: bytes,
        buffered: int,
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
        known_id: str | None = None
        dispatched = False
        try:
            try:
                await asyncio.wait_for(asyncio.shield(job.started), self.config.queue_wait_seconds)
            except asyncio.TimeoutError:
                self.queue.cancel(job)
                if not response.done():
                    response.set_exception(web.HTTPGatewayTimeout(text="AI Proxy queue wait expired"))
                return
            started = True
            if job.cancelled or self.queue.closed:
                self.logger(
                    f"{profile.name} HTTP request {job.request_id} cancelled before dispatch"
                )
                if not response.done():
                    response.set_exception(web.HTTPServiceUnavailable(text="AI Proxy is shutting down"))
                return
            if job.start_error:
                if not response.done():
                    response.set_exception(web.HTTPServiceUnavailable(text=job.start_error, headers={"Retry-After": "30"}))
                return
            try:
                forwarded_body, known_id, body_changed = _prepare_prompt_id(body, request.method, request.path)
            except ValueError as exc:
                if not response.done():
                    response.set_exception(web.HTTPBadRequest(text=str(exc)))
                return
            if known_id is None:
                if not response.done():
                    response.set_exception(web.HTTPBadRequest(text="ComfyUI prompt must contain a workflow object"))
                return
            if self.store:
                try:
                    self.store.create(job.request_id, profile.name, known_id)
                except FileExistsError:
                    if not response.done():
                        response.set_exception(web.HTTPConflict(text="prompt_id is already tracked"))
                    return
            headers = _forward_headers(request.headers, request=True)
            if body_changed:
                headers = [
                    (name, value)
                    for name, value in headers
                    if name.casefold() != "content-length"
                ]
            downstream: web.StreamResponse | None = None
            settlement_deadline = asyncio.get_running_loop().time() + self.config.upstream_timeout_seconds
            try:
                dispatched = True
                job.dispatched = True
                if self.store:
                    self.store.update(job.request_id, "dispatched")
                async with self._session.request(
                    request.method,
                    f"{self._upstream}{request.raw_path}",
                    headers=headers,
                    data=forwarded_body,
                    allow_redirects=False,
                ) as upstream:
                    status = upstream.status
                    wire_body = await _read_bounded(upstream.content, self.config.max_body_bytes)
                    upstream_body = _decode_body(wire_body, upstream.headers.get("Content-Encoding"), self.config.max_body_bytes)
                    if 200 <= status < 300:
                        returned_id = _extract_prompt_id(upstream_body)
                        if returned_id is not None and returned_id != known_id:
                            self.logger(
                                f"{profile.name} upstream returned prompt id {returned_id} "
                                f"different from preassigned {known_id}; tracking the returned id"
                            )
                            known_id = returned_id
                            if self.store:
                                try:
                                    self.store.reconcile_prompt_id(job.request_id, returned_id)
                                except FileExistsError:
                                    raise web.HTTPConflict(text="upstream returned an already tracked prompt_id") from None
                        else:
                            known_id = returned_id or known_id
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
                        if not response.done():
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
                        remaining = max(0.0, settlement_deadline - asyncio.get_running_loop().time())
                        history = await self._wait_for_prompt_completion(known_id, request.path, timeout=remaining)
                        if history is not None:
                            await self._archive_completion(job.request_id, known_id, history)
                        else:
                            await self._cancel_and_reconcile(job.request_id, known_id, request.path)
                    elif self.store:
                        self.store.update(job.request_id, "failed", outcome="rejected", error=f"upstream status {status}")
            except asyncio.TimeoutError:
                self.logger(
                    f"{profile.name} upstream failed for request {job.request_id}"
                    + ("; reconciling by prompt id" if dispatched and known_id else "")
                )
                if dispatched and known_id:
                    remaining = max(0.0, settlement_deadline - asyncio.get_running_loop().time())
                    history = await self._wait_for_prompt_completion(known_id, request.path, timeout=remaining)
                    if history is not None:
                        await self._archive_completion(job.request_id, known_id, history)
                    else:
                        await self._cancel_and_reconcile(job.request_id, known_id, request.path)
                if not response.done():
                    response.set_exception(
                        web.HTTPGatewayTimeout(text="upstream timed out")
                    )
            except (aiohttp.ClientError, ConnectionError, OSError, web.HTTPException):
                self.logger(
                    f"{profile.name} upstream failed for request {job.request_id}"
                    + ("; reconciling by prompt id" if dispatched and known_id else "")
                )
                if dispatched and known_id:
                    remaining = max(0.0, settlement_deadline - asyncio.get_running_loop().time())
                    history = await self._wait_for_prompt_completion(known_id, request.path, timeout=remaining)
                    if history is not None:
                        await self._archive_completion(job.request_id, known_id, history)
                    else:
                        await self._cancel_and_reconcile(job.request_id, known_id, request.path)
                if downstream is not None:
                    try:
                        downstream.force_close()
                    except Exception:
                        pass
                else:
                    if not response.done():
                        response.set_exception(
                            web.HTTPBadGateway(text="upstream request failed")
                        )
        except asyncio.CancelledError:
            if self.store and dispatched:
                self.store.update(job.request_id, "failed", outcome="shutdown", error="proxy stopped during reconciliation")
            if not response.done():
                response.cancel()
            raise
        except Exception as exc:
            if not response.done():
                response.set_exception(exc)
        finally:
            if started and not job.finished.done():
                self.queue.finish(job)
            await self.admission.leave(buffered)

    async def _wait_for_prompt_completion(self, prompt_id: str, path: str, *, timeout: float | None = None) -> bytes | None:
        """Poll upstream history until prompt_id has a terminal entry.

        Bounded by a single explicit deadline of ``upstream_timeout_seconds``.
        Returns the decoded history once a parsed ``{prompt_id: {...}}`` entry exists; any
        terminal entry (including errors or interruptions) frees the GPU.
        """
        assert self._session is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout if timeout is not None else self.config.upstream_timeout_seconds)
        url = f"{self._upstream}{_history_path(path)}/{prompt_id}"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                request_timeout = aiohttp.ClientTimeout(total=max(0.05, remaining))
                async with self._session.get(url, timeout=request_timeout) as history:
                    if 200 <= history.status < 300:
                        wire = await _read_bounded(history.content, self.config.max_body_bytes)
                        body = _decode_body(wire, history.headers.get("Content-Encoding"), self.config.max_body_bytes)
                        if self._parse_terminal_entry(body, prompt_id):
                            return body
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError, web.HTTPException):
                pass
            await asyncio.sleep(min(self.config.settle_poll_seconds, remaining))

    async def _cancel_and_reconcile(self, request_id: str, prompt_id: str, path: str) -> None:
        assert self._session is not None
        if self.store:
            self.store.update(request_id, "cancelling", error="generation settlement deadline expired")
        try:
            async with self._session.post(f"{self._upstream}/interrupt") as interrupt:
                if interrupt.status >= 300:
                    self.logger(f"ComfyUI cancellation returned HTTP {interrupt.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.logger(f"ComfyUI cancellation failed: {exc}")
        history = await self._wait_for_prompt_completion(
            prompt_id, path, timeout=self.config.cancellation_grace_seconds
        )
        if history is not None:
            await self._archive_completion(request_id, prompt_id, history)
            return
        if self.backend_manager and self.backend_manager.config.enabled and self.backend_manager.ownership == "proxy":
            await self.backend_manager.stop_comfyui()
            if self.store:
                self.store.update(request_id, "failed", outcome="terminated", error="backend terminated after cancellation grace")
            return
        if self.store:
            self.store.update(request_id, "blocked", outcome="unresolved", error="external backend did not settle; intervention required")
        self.logger(f"ComfyUI prompt {prompt_id} is unresolved; external GPU execution remains blocked")
        # Never free an external, unresolved GPU slot. Service shutdown cancels this wait.
        await asyncio.Event().wait()

    async def _archive_completion(self, request_id: str, prompt_id: str, history_body: bytes) -> None:
        if not self.store:
            return
        try:
            self.store.evict(self.retention_days, self.cache_max_bytes)
            payload = json.loads(history_body)
            entry = payload[prompt_id]
            status = entry.get("status", {}) if isinstance(entry, dict) else {}
            upstream_succeeded = (
                isinstance(status, dict)
                and status.get("completed") is True
                and status.get("status_str") in {"success", "succeeded"}
            )
            outputs = entry.get("outputs", {})
            if not isinstance(outputs, dict):
                raise ValueError("history outputs are not an object")
            artifacts: list[dict[str, str]] = []
            for node in outputs.values():
                if not isinstance(node, dict):
                    continue
                for value in node.values():
                    if not isinstance(value, list):
                        continue
                    for item in value:
                        if not isinstance(item, dict) or "filename" not in item:
                            continue
                        filename = item.get("filename")
                        subfolder = item.get("subfolder", "")
                        kind = item.get("type", "output")
                        if not all(isinstance(part, str) for part in (filename, subfolder, kind)) or kind not in {"output", "temp"}:
                            raise ValueError("unsupported ComfyUI artifact reference")
                        artifacts.append({"filename": filename, "subfolder": subfolder, "type": kind})
            for artifact in artifacts:
                await self._archive_artifact(prompt_id, artifact)
            self.store.update(
                request_id,
                "completed" if upstream_succeeded else "failed",
                outcome="succeeded" if upstream_succeeded else "upstream_error",
                history=history_body,
                error=None if upstream_succeeded else str(status.get("status_str") or "ComfyUI execution failed"),
            )
            self.store.evict(self.retention_days, self.cache_max_bytes)
            self._cache_error = None
        except (OSError, ValueError, KeyError, json.JSONDecodeError, aiohttp.ClientError) as exc:
            self._cache_error = str(exc)
            self.store.update(request_id, "failed", outcome="archive_failed", error=str(exc))
            raise BackendUnavailable(f"Could not preserve ComfyUI result: {exc}") from exc

    async def _archive_artifact(self, prompt_id: str, artifact: dict[str, str]) -> None:
        assert self._session is not None and self.store is not None
        params = {"filename": artifact["filename"], "subfolder": artifact["subfolder"], "type": artifact["type"]}
        safe_name = Path(artifact["filename"]).name
        if safe_name != artifact["filename"] or not safe_name:
            raise ValueError("unsafe artifact filename")
        target_dir = self.store.cache_dir / prompt_id
        target_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(f"{artifact['type']}\0{artifact['subfolder']}\0{artifact['filename']}".encode()).hexdigest()[:16]
        target = target_dir / f"{cache_key}-{safe_name}"
        temp = target.with_suffix(target.suffix + ".partial")
        size = 0
        existing = self.store.cache_bytes()
        try:
            async with self._session.get(f"{self._upstream}/view", params=params) as response:
                if response.status != 200:
                    raise ValueError(f"artifact fetch returned HTTP {response.status}")
                with temp.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if existing + size > self.cache_max_bytes:
                            raise ValueError("artifact cache capacity exceeded")
                        handle.write(chunk)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        self.store.add_artifact(prompt_id, artifact["filename"], artifact["subfolder"], artifact["type"], target, size)

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
                if self.profile.name == "ollama" and request_id is not None:
                    try:
                        async for _chunk in upstream.content.iter_chunked(64 * 1024):
                            pass
                        return downstream
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        pass
                downstream.force_close()
            raise
        except asyncio.TimeoutError:
            if downstream is None:
                raise web.HTTPGatewayTimeout(text="upstream timed out") from None
            downstream.force_close()
            raise web.HTTPGatewayTimeout(text="upstream stream timed out ambiguously") from None
        except (aiohttp.ClientError, ConnectionError):
            if downstream is None:
                raise web.HTTPBadGateway(text="upstream request failed") from None
            downstream.force_close()
            raise web.HTTPBadGateway(text="upstream stream failed ambiguously") from None


def _extract_prompt_id(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = payload.get("prompt_id") if isinstance(payload, dict) else None
    return value if _is_valid_upstream_prompt_id(value) else None


def _decode_body(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
    normalized = (encoding or "").strip().casefold()
    try:
        if normalized == "gzip":
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = inflater.decompress(body, max_bytes + 1)
            if len(decoded) <= max_bytes:
                decoded += inflater.flush(max_bytes + 1 - len(decoded))
        elif normalized == "deflate":
            inflater = zlib.decompressobj()
            decoded = inflater.decompress(body, max_bytes + 1)
            if len(decoded) <= max_bytes:
                decoded += inflater.flush(max_bytes + 1 - len(decoded))
        elif normalized in {"", "identity"}:
            decoded = body
        else:
            raise web.HTTPBadGateway(text=f"unsupported upstream content encoding: {normalized}")
    except (OSError, zlib.error) as exc:
        raise web.HTTPBadGateway(text="invalid compressed upstream response") from exc
    if len(decoded) > max_bytes:
        raise web.HTTPBadGateway(text="decoded upstream response exceeds configured limit")
    return decoded


async def _read_bounded(content: aiohttp.StreamReader, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise web.HTTPBadGateway(text="upstream response exceeds configured limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def run_http_proxies(
    proxy: Proxy,
    configs: list[HttpProxyConfig],
    poll_seconds: float,
    http_logger: Callable[[str], None] | None = None,
    runtime_config: RuntimeConfig | None = None,
    runtime_state: HttpRuntimeState | None = None,
) -> None:
    if not configs:
        raise ValueError("At least one HTTP listener configuration is required")
    for config in configs:
        config.validate()
    for index, config in enumerate(configs):
        for other in configs[index + 1:]:
            if config.listen_port == other.listen_port and _same_endpoint(config.listen_host, other.listen_host):
                raise ValueError(f"Duplicate or overlapping HTTP listener {config.listen_host}:{config.listen_port}")
        for upstream_config in configs:
            parsed = urlsplit(upstream_config.upstream)
            upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if config.listen_port == upstream_port and parsed.hostname and _same_endpoint(config.listen_host, parsed.hostname):
                raise ValueError(
                    f"HTTP listener {config.listen_host}:{config.listen_port} overlaps {upstream_config.profile.name} upstream"
                )

    runtime = runtime_config
    queue = HttpQueue(
        max_resource_streak=runtime.max_resource_streak if runtime else 5,
        oldest_override_seconds=runtime.oldest_request_seconds if runtime else 60.0,
    )
    logger = http_logger or proxy.logger
    store = ExecutionStore(Path(runtime.runtime_dir)) if runtime else None
    manager = BackendManager(runtime.managed_comfyui, Path(runtime.runtime_dir), logger) if runtime else None
    admission = AdmissionController(
        runtime.max_admitted_requests if runtime else min(c.max_admitted_requests for c in configs),
        runtime.max_buffered_body_bytes if runtime else min(c.max_buffered_body_bytes for c in configs),
    )
    services = [
        HttpProxyService(
            config,
            queue,
            logger,
            admission=admission,
            store=store,
            backend_manager=manager,
            status_provider=proxy.status,
            retention_days=runtime.retention_days if runtime else 7,
            cache_max_bytes=runtime.cache_max_bytes if runtime else 10 * 1024 * 1024 * 1024,
        )
        for config in configs
    ]
    if runtime_state is not None:
        runtime_state.queue = queue
        runtime_state.services = services
        runtime_state.backend_manager = manager
        runtime_state.store = store
        runtime_state.listener_states = {
            config.profile.name: "starting" for config in configs
        }
    started: list[HttpProxyService] = []
    filesystem_ready = True
    try:
        proxy.ensure_layout()
    except OSError as exc:
        filesystem_ready = False
        proxy._filesystem_degraded = str(exc)
        proxy.logger(f"Filesystem queue unavailable; starting API listeners only: {exc}")
    proxy.logger(f"Queue root: {proxy.root}")
    with proxy.lock():
        proxy.logger(f"Lock acquired: {proxy.local_runtime_dir / 'proxy.lock'}")
        recovered = proxy.recover_interrupted() if filesystem_ready else 0
        if recovered:
            proxy.logger(f"Recovered {recovered} interrupted job(s)")
        if store and manager:
            pending_ollama = [record for record in store.pending() if record.get("backend") == "ollama"]
            if pending_ollama:
                ollama_upstream = next((c.upstream for c in configs if c.profile.name == "ollama"), None)
                if ollama_upstream:
                    try:
                        await manager.reconcile_ollama(ollama_upstream)
                    except BackendUnavailable as exc:
                        manager.block(f"Ollama restart reconciliation failed: {exc}")
                        for record in pending_ollama:
                            store.update(record["request_id"], "blocked", outcome="unresolved", error=str(exc))
                    else:
                        for record in pending_ollama:
                            store.update(record["request_id"], "failed", outcome="reconciled", error="abandoned downstream stream was unloaded after restart")
                else:
                    manager.block("An unresolved Ollama execution exists but no Ollama listener is configured for reconciliation")
        try:
            for service in services:
                await service.start()
                started.append(service)
                if runtime_state is not None:
                    runtime_state.listener_states[service.profile.name] = "listening"
            reconciliation_service = next((service for service in services if service.profile.name == "comfyui"), services[0])
            await reconciliation_service.reconcile_records()
            if runtime_state is not None:
                runtime_state.ready.set()
        except BaseException:
            if runtime_state is not None:
                for config in configs:
                    if runtime_state.listener_states.get(config.profile.name) != "listening":
                        runtime_state.listener_states[config.profile.name] = "error"
            for service in reversed(started):
                try:
                    await service.stop()
                except BaseException:
                    pass
                if runtime_state is not None:
                    runtime_state.listener_states[service.profile.name] = "stopped"
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
                backend_manager=manager,
                ollama_upstream=next((c.upstream for c in configs if c.profile.name == "ollama"), None),
            )
        finally:
            if runtime_state is not None:
                for service in services:
                    runtime_state.listener_states[service.profile.name] = "stopped"
            for service in reversed(started):
                try:
                    await service.stop()
                except BaseException:
                    pass
            if manager:
                await manager.close()
            if store and runtime:
                store.evict(runtime.retention_days, runtime.cache_max_bytes)


async def run_http_proxy(
    proxy: Proxy,
    config: HttpProxyConfig,
    poll_seconds: float,
    http_logger: Callable[[str], None] | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> None:
    await run_http_proxies(proxy, [config], poll_seconds, http_logger, runtime_config)
