from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import aiohttp

from .runtime import ManagedComfyUIConfig


class BackendUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendStatus:
    state: str
    ownership: str
    blocked_reason: str | None
    active_backend: str | None


class BackendManager:
    """Own process state and safe GPU transitions; it never kills external services."""

    def __init__(self, config: ManagedComfyUIConfig, runtime_dir: Path, logger: Callable[[str], None]) -> None:
        self.config = config
        self.runtime_dir = runtime_dir
        self.logger = logger
        self.state = "stopped" if config.enabled else "ready"
        self.ownership = "proxy" if config.enabled else "external"
        self.blocked_reason: str | None = None
        self.active_backend: str | None = None
        self.active_job: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_created_at: float | None = None
        self._job_handle: int | None = None
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0
        self._logs: deque[str] = deque(maxlen=200)
        self._idle_task: asyncio.Task[None] | None = None
        self._log_thread: threading.Thread | None = None
        self._transitions: deque[dict[str, object]] = deque(maxlen=50)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self._transitions.append({"at": time.time(), "from": self.state, "to": state})
            self.state = state

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "ownership": self.ownership,
            "blocked_reason": self.blocked_reason,
            "active_backend": self.active_backend,
            "active_job": self.active_job,
            "pid": self._process.pid if self._process and self._process.poll() is None else None,
            "process_created_at": self._process_created_at,
            "recent_logs": list(self._logs)[-20:],
            "transitions": list(self._transitions),
        }

    def block(self, reason: str) -> None:
        self.blocked_reason = reason
        self._set_state("blocked")

    async def prepare_for(self, backend: str | None, *, ollama_upstream: str | None = None, job_id: str | None = None) -> None:
        if self.blocked_reason:
            raise BackendUnavailable(self.blocked_reason)
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        if backend == "comfyui":
            await self.ensure_comfyui(ollama_upstream)
        elif backend == "ollama" and self.config.enabled:
            await self.stop_comfyui()
        self.active_backend = backend
        self.active_job = job_id
        if backend and (not self.config.enabled or backend != "comfyui" or self.state == "ready"):
            self._set_state("busy")

    async def released(self, backend: str | None) -> None:
        self.active_backend = None
        self.active_job = None
        if self.config.enabled and backend == "comfyui" and self._process and self._process.poll() is None:
            self._set_state("ready")
            self._idle_task = asyncio.create_task(self._idle_stop(), name="comfyui-idle-stop")
        elif self.config.enabled:
            self._set_state("stopped")
        elif not self.config.enabled:
            self._set_state("ready")

    async def _idle_stop(self) -> None:
        try:
            await asyncio.sleep(self.config.idle_shutdown_seconds)
            if self.active_backend is None:
                await self.stop_comfyui()
                self.logger("Stopped managed ComfyUI after its idle interval")
        except asyncio.CancelledError:
            return

    async def ensure_comfyui(self, ollama_upstream: str | None = None) -> None:
        if not self.config.enabled:
            return
        async with self._lock:
            if self._process and self._process.poll() is None and await self._ready():
                self._set_state("ready")
                return
            if await self._ready():
                self._set_state("blocked")
                self.ownership = "external"
                self.blocked_reason = (
                    "A manually started ComfyUI instance occupies the managed endpoint. "
                    "Stop it before submitting managed GPU work."
                )
                raise BackendUnavailable(self.blocked_reason)
            now = asyncio.get_running_loop().time()
            if now < self._cooldown_until:
                raise BackendUnavailable("Managed ComfyUI startup is cooling down after a failure")
            if ollama_upstream:
                await self._unload_ollama(ollama_upstream)
            self._set_state("starting")
            self.ownership = "proxy"
            self.blocked_reason = None
            try:
                self._start_process()
                deadline = now + self.config.startup_deadline_seconds
                while asyncio.get_running_loop().time() < deadline:
                    if self._process is None or self._process.poll() is not None:
                        raise BackendUnavailable("Managed ComfyUI exited before becoming ready")
                    if await self._ready():
                        self._set_state("ready")
                        return
                    await asyncio.sleep(0.25)
                raise BackendUnavailable("Managed ComfyUI readiness deadline expired")
            except BaseException:
                self._set_state("failed")
                self._cooldown_until = asyncio.get_running_loop().time() + self.config.startup_cooldown_seconds
                await self._terminate_owned()
                raise

    def _start_process(self) -> None:
        executable = str(Path(self.config.executable or "").resolve())
        command = [executable]
        if self.config.script:
            command.append(str(Path(self.config.script).resolve()))
        command.extend(self.config.arguments)
        if self.config.model_config:
            command.extend(["--extra-model-paths-config", str(Path(self.config.model_config).resolve())])
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x00000004 | 0x01000000  # suspended, break away, then assign
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        log_path = self.runtime_dir / "comfyui.log"
        self._rotate_log(log_path)
        self._process = subprocess.Popen(
            command,
            cwd=self.config.working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=flags,
            startupinfo=startupinfo,
        )
        assert self._process.stdout is not None
        self._log_thread = threading.Thread(
            target=self._capture_logs,
            args=(self._process.stdout, log_path),
            name="comfyui-log-capture",
            daemon=True,
        )
        self._log_thread.start()
        self._process_created_at = time.time()
        if os.name == "nt":
            self._assign_windows_job(self._process)
            self._resume_windows_process(self._process.pid)
        self.logger(f"Started managed ComfyUI process {self._process.pid}")

    def _assign_windows_job(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            process.terminate()
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            kernel32.CloseHandle(job)
            process.terminate()
            raise OSError(ctypes.get_last_error(), "Could not contain managed ComfyUI process")
        self._job_handle = job

    @staticmethod
    def _resume_windows_process(pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "Could not enumerate managed process threads")
        entry = THREADENTRY32(dwSize=ctypes.sizeof(THREADENTRY32))
        resumed = False
        try:
            found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                    if thread:
                        try:
                            kernel32.ResumeThread(thread)
                            resumed = True
                        finally:
                            kernel32.CloseHandle(thread)
                    break
                found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        if not resumed:
            raise OSError(ctypes.get_last_error(), "Could not resume managed ComfyUI process")

    def _rotate_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size < self.config.log_max_bytes:
            return
        for index in range(self.config.log_backups, 0, -1):
            source = path if index == 1 else path.with_suffix(f".log.{index - 1}")
            target = path.with_suffix(f".log.{index}")
            if source.exists():
                target.unlink(missing_ok=True)
                source.replace(target)

    def _capture_logs(self, stream, path: Path) -> None:
        handle = path.open("ab", buffering=0)
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                handle.write(chunk)
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    self._logs.append(line[-2000:])
                if handle.tell() >= self.config.log_max_bytes:
                    handle.close()
                    self._rotate_log(path)
                    handle = path.open("ab", buffering=0)
        finally:
            handle.close()
            stream.close()

    async def _ready(self) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=1.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.config.upstream.rstrip('/')}/system_stats") as response:
                    return 200 <= response.status < 300
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def _unload_ollama(self, upstream: str) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(f"{upstream.rstrip('/')}/api/ps") as response:
                    payload = await response.json()
                models = payload.get("models", []) if isinstance(payload, dict) else []
                for model in models:
                    name = model.get("name") or model.get("model") if isinstance(model, dict) else None
                    if name:
                        async with session.post(f"{upstream.rstrip('/')}/api/generate", json={"model": name, "keep_alive": 0}) as response:
                            if response.status >= 300:
                                raise BackendUnavailable(f"Ollama refused to unload {name}")
                async with session.get(f"{upstream.rstrip('/')}/api/ps") as response:
                    remaining = await response.json()
                if isinstance(remaining, dict) and remaining.get("models"):
                    raise BackendUnavailable("Ollama models remain resident after unload")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
                raise BackendUnavailable(f"Could not verify Ollama unload: {exc}") from exc

    async def reconcile_ollama(self, upstream: str) -> None:
        await self._unload_ollama(upstream)

    async def stop_comfyui(self) -> None:
        async with self._lock:
            if self._process is None:
                if await self._ready():
                    self._set_state("blocked")
                    self.ownership = "external"
                    self.blocked_reason = "External ComfyUI is running; the proxy will not stop it"
                    raise BackendUnavailable(self.blocked_reason)
                self._set_state("stopped")
                return
            self._set_state("stopping")
            await self._terminate_owned()
            self._set_state("stopped")

    async def _terminate_owned(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), self.config.shutdown_deadline_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)
        self._process = None
        if self._log_thread:
            await asyncio.to_thread(self._log_thread.join, 1.0)
            self._log_thread = None
        if self._job_handle and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._job_handle)
            self._job_handle = None

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        if self.config.enabled and self._process is not None:
            await self.stop_comfyui()
