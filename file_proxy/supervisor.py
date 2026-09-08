from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import threading

from .engine import Proxy
from .http_proxy import HttpProxyConfig, HttpRuntimeState, run_http_proxies
from .runtime import RuntimeConfig


@dataclass(frozen=True)
class LogEntry:
    sequence: int
    timestamp: datetime
    source: str
    level: str
    message: str


@dataclass(frozen=True)
class ListenerSnapshot:
    profile: str
    host: str
    port: int
    upstream: str
    state: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: str
    error: str | None
    listeners: tuple[ListenerSnapshot, ...]
    filesystem_counts: dict[str, int]
    http_queued: int
    http_oldest_wait_seconds: float
    admitted_requests: int
    buffered_body_bytes: int
    active_backend: str | None
    active_job: str | None
    filesystem_degraded: str | None
    backend_status: dict[str, object] | None
    cache_status: dict[str, object] | None
    logs: tuple[LogEntry, ...]


class RuntimeSupervisor:
    """Own a restartable proxy runtime for the terminal dashboard."""

    def __init__(
        self,
        proxy: Proxy,
        configs: list[HttpProxyConfig],
        runtime_config: RuntimeConfig,
        *,
        poll_seconds: float = 1.0,
        log_capacity: int = 2_000,
    ) -> None:
        self.proxy = proxy
        self.configs = tuple(configs)
        self.runtime_config = runtime_config
        self.poll_seconds = poll_seconds
        self.state = "stopped"
        self.error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._http_state: HttpRuntimeState | None = None
        self._file_stop: threading.Event | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._logs: deque[LogEntry] = deque(maxlen=log_capacity)
        self._log_lock = threading.Lock()
        self._log_sequence = 0
        self.proxy.logger = lambda message: self.log("file-proxy", message)

    @property
    def transitioning(self) -> bool:
        return self._lifecycle_lock.locked()

    def log(self, source: str, message: str, level: str | None = None) -> None:
        normalized = message.casefold()
        inferred = level or (
            "error"
            if any(word in normalized for word in ("error", "failed", "invalid", "unavailable", "blocked"))
            else "warning"
            if any(word in normalized for word in ("warning", "degraded", "held", "stopping"))
            else "success"
            if any(word in normalized for word in ("ready", "listening", "finished", "recovered"))
            else "info"
        )
        with self._log_lock:
            self._log_sequence += 1
            self._logs.append(
                LogEntry(
                    sequence=self._log_sequence,
                    timestamp=datetime.now().astimezone(),
                    source=source,
                    level=inferred,
                    message=message,
                )
            )

    async def start(self) -> bool:
        if self.transitioning:
            self.log("dashboard", "Start ignored; a lifecycle transition is already in progress.", "warning")
            return False
        async with self._lifecycle_lock:
            if self.state in {"ready", "busy", "degraded", "starting"}:
                self.log("dashboard", "Start ignored; proxy is already running.", "warning")
                return False
            return await self._start_unlocked()

    async def _start_unlocked(self) -> bool:
        self.state = "starting"
        self.error = None
        self.proxy._filesystem_degraded = None
        self.log("dashboard", "Starting proxy runtime.")
        try:
            if self.configs:
                state = HttpRuntimeState()
                self._http_state = state
                self._task = asyncio.create_task(
                    run_http_proxies(
                        self.proxy,
                        list(self.configs),
                        self.poll_seconds,
                        lambda message: self.log("http-proxy", message),
                        self.runtime_config,
                        state,
                    ),
                    name="ai-proxy-runtime",
                )
                ready = asyncio.create_task(state.ready.wait(), name="ai-proxy-ready")
                done, _pending = await asyncio.wait(
                    {self._task, ready}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._task in done:
                    ready.cancel()
                    await self._task
                else:
                    ready.result()
            else:
                ready_event = asyncio.Event()
                self._file_stop = threading.Event()
                loop = asyncio.get_running_loop()
                self._task = asyncio.create_task(
                    asyncio.to_thread(self._run_filesystem, loop, ready_event),
                    name="ai-proxy-filesystem-runtime",
                )
                ready = asyncio.create_task(ready_event.wait(), name="ai-proxy-ready")
                done, _pending = await asyncio.wait(
                    {self._task, ready}, return_when=asyncio.FIRST_COMPLETED
                )
                if self._task in done:
                    ready.cancel()
                    await self._task
                else:
                    ready.result()
            self.state = "ready"
            self.log("dashboard", "Proxy runtime is ready.", "success")
            for config in self.configs:
                profile_label = "ComfyUI" if config.profile.name == "comfyui" else config.profile.name.title()
                self.log(
                    "dashboard",
                    f"{profile_label} proxy listener confirmed at "
                    f"{config.listen_host}:{config.listen_port}.",
                    "success",
                )
            if self.runtime_config.managed_comfyui.enabled:
                self.log(
                    "dashboard",
                    "Managed ComfyUI backend is stopped until a ComfyUI job arrives; "
                    "the proxy listener remains ready.",
                )
            if self._task is not None:
                self._task.add_done_callback(self._runtime_done)
            return True
        except Exception as exc:
            self.state = "error"
            self.error = str(exc)
            if self._http_state is not None:
                for config in self.configs:
                    if self._http_state.listener_states.get(config.profile.name) == "starting":
                        self._http_state.listener_states[config.profile.name] = "error"
            self.log("dashboard", f"Proxy startup failed: {exc}", "error")
            await self._stop_runtime_objects()
            return False

    def _run_filesystem(
        self, loop: asyncio.AbstractEventLoop, ready_event: asyncio.Event
    ) -> None:
        assert self._file_stop is not None
        self.proxy.ensure_layout()
        self.proxy.logger(f"Queue root: {self.proxy.root}")
        with self.proxy.lock():
            self.proxy.logger(f"Lock acquired: {self.proxy.local_runtime_dir / 'proxy.lock'}")
            recovered = self.proxy.recover_interrupted()
            if recovered:
                self.proxy.logger(f"Recovered {recovered} interrupted job(s)")
            loop.call_soon_threadsafe(ready_event.set)
            self.proxy.logger(f"Ready; polling every {self.poll_seconds:g}s.")
            idle = False
            while not self._file_stop.is_set():
                if self.proxy.process_once() is None:
                    if not idle:
                        self.proxy.logger("Queue is idle")
                        idle = True
                    self._file_stop.wait(self.poll_seconds)
                else:
                    idle = False

    def _runtime_done(self, task: asyncio.Task[None]) -> None:
        if self.state in {"stopping", "stopped"} or task.cancelled():
            return
        try:
            failure = task.exception()
        except asyncio.CancelledError:
            return
        self.state = "error"
        self.error = str(failure) if failure else "Proxy runtime stopped unexpectedly"
        self.log("dashboard", self.error, "error")

    async def stop(self) -> bool:
        if self.transitioning:
            self.log("dashboard", "Stop ignored; a lifecycle transition is already in progress.", "warning")
            return False
        async with self._lifecycle_lock:
            if self.state == "stopped":
                self.log("dashboard", "Stop ignored; proxy is already stopped.", "warning")
                return False
            self.state = "stopping"
            self.log("dashboard", "Stopping proxy runtime safely.", "warning")
            await self._stop_runtime_objects()
            self.state = "stopped"
            self.error = None
            self.log("dashboard", "Proxy runtime stopped.")
            return True

    async def _stop_runtime_objects(self) -> None:
        task = self._task
        if self._http_state is not None:
            for service in self._http_state.services:
                try:
                    await service.stop_accepting()
                except Exception as exc:
                    self.log("dashboard", f"Could not close a listener cleanly: {exc}", "warning")
        if self._file_stop is not None:
            self._file_stop.set()
        elif task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._file_stop = None

    async def restart(self) -> bool:
        if self.transitioning:
            self.log("dashboard", "Restart ignored; a lifecycle transition is already in progress.", "warning")
            return False
        async with self._lifecycle_lock:
            self.log("dashboard", "Restarting proxy runtime.")
            if self.state != "stopped":
                self.state = "stopping"
                await self._stop_runtime_objects()
            return await self._start_unlocked()

    async def snapshot(self) -> RuntimeSnapshot:
        try:
            filesystem = await asyncio.to_thread(self.proxy.status)
        except OSError as exc:
            filesystem = {"counts": {}, "jobs": [], "degraded": str(exc)}
        raw_counts = filesystem.get("counts", {})
        counts = {
            key: int(raw_counts.get(key, 0))
            for key in ("queued", "running", "succeeded", "failed", "invalid")
        }
        http_state = self._http_state
        queue = http_state.queue.snapshot() if http_state and http_state.queue else {}
        manager = http_state.backend_manager if http_state else None
        backend_status = manager.status() if manager else None
        store = http_state.store if http_state else None
        cache_status: dict[str, object] | None = None
        if store:
            try:
                cache_status = await asyncio.to_thread(store.status)
            except OSError as exc:
                cache_status = {"error": str(exc)}
        if http_state and http_state.services:
            cache_error = next(
                (service._cache_error for service in http_state.services if service._cache_error),
                None,
            )
            if cache_error:
                cache_status = {**(cache_status or {}), "error": cache_error}
        admission = http_state.services[0].admission if http_state and http_state.services else None
        active_backend = (
            str(backend_status.get("active_backend"))
            if backend_status and backend_status.get("active_backend")
            else None
        )
        active_job = (
            str(backend_status.get("active_job"))
            if backend_status and backend_status.get("active_job")
            else None
        )
        if active_job is None:
            running = next(
                (item for item in filesystem.get("jobs", []) if item.get("status") == "running"),
                None,
            )
            if running:
                active_job = f"{running['subscriber_id']}/{running['job_id']}"
                active_backend = active_backend or "filesystem"
        degraded = filesystem.get("degraded")
        effective_state = self.state
        if effective_state in {"ready", "busy", "degraded"}:
            if (
                degraded
                or (backend_status and backend_status.get("blocked_reason"))
                or (cache_status and cache_status.get("error"))
            ):
                effective_state = "degraded"
            elif active_job or counts["running"]:
                effective_state = "busy"
            else:
                effective_state = "ready"
        listeners = tuple(
            ListenerSnapshot(
                profile=config.profile.name,
                host=config.listen_host,
                port=config.listen_port,
                upstream=config.upstream,
                state=(
                    http_state.listener_states.get(config.profile.name, "stopped")
                    if http_state
                    else "stopped"
                ),
            )
            for config in self.configs
        )
        with self._log_lock:
            logs = tuple(self._logs)
        return RuntimeSnapshot(
            state=effective_state,
            error=self.error,
            listeners=listeners,
            filesystem_counts=counts,
            http_queued=int(queue.get("queued", 0)),
            http_oldest_wait_seconds=float(queue.get("oldest_wait_seconds", 0.0)),
            admitted_requests=admission.requests if admission else 0,
            buffered_body_bytes=admission.bytes if admission else 0,
            active_backend=active_backend,
            active_job=active_job,
            filesystem_degraded=str(degraded) if degraded else None,
            backend_status=backend_status,
            cache_status=cache_status,
            logs=logs,
        )
