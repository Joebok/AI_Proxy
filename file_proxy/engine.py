from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
from collections.abc import Callable
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .models import InvalidJob, JobManifest, JobNotReady, ProxyResult, QueueJob, iso_utc, utc_now
from .registry import Registry
from .scheduler import HttpQueue
from .backend import BackendManager, BackendUnavailable
from .validation import build_file_inventory, find_link_like, is_link_like, remove_link_like, validate_job_folder
from .storage_health import StorageHealth, StorageUnavailable


class ProxyAlreadyRunning(RuntimeError):
    pass


class AnswerCollision(RuntimeError):
    pass


class RuntimeLock:
    def __init__(self, path: Path):
        self.path = path
        self._owned = False

    def __enter__(self) -> "RuntimeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if attempt or self._lock_owner_is_active():
                    raise ProxyAlreadyRunning(f"proxy lock already exists: {self.path}") from exc
                self.path.unlink(missing_ok=True)
        else:
            raise ProxyAlreadyRunning(f"proxy lock already exists: {self.path}")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            from .worker_host import process_identity
            json.dump(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "started_at": iso_utc(utc_now()),
                    "process_identity": process_identity(os.getpid()),
                },
                handle,
            )
        self._owned = True
        return self

    def _lock_owner_is_active(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            hostname = str(data.get("hostname") or "")
            if hostname and hostname.casefold() != socket.gethostname().casefold():
                return True
            pid = int(data["pid"])
            if pid <= 0:
                return False
            if os.name == "nt":
                from .worker_host import process_identity, process_alive
                return process_alive(data["process_identity"]) if data.get("process_identity") else process_identity(pid) is not None
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return self.path.exists()

    def __exit__(self, *_: object) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _replace_with_retry(temp, path)


def _replace_with_retry(source: Path, destination: Path, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    delay = 0.05
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _inventory_with_retry(path: Path, timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    delay = 0.05
    while True:
        try:
            return build_file_inventory(path, {"proxy_result.json"})
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


class Proxy:
    def __init__(
        self,
        root: Path,
        registry: Registry,
        logger: Callable[[str], None] | None = None,
        sync_grace_seconds: float = 300.0,
        max_resource_streak: int = 5,
        forge_upstream: str | None = None,
        forge_unload_timeout_seconds: float = 30.0,
        local_runtime_dir: Path | None = None,
        forge_cleanup_required: bool = False,
        cloud_concurrency: int = 2,
        codex_models=None,
    ):
        if max_resource_streak <= 0:
            raise ValueError("max_resource_streak must be positive")
        if forge_unload_timeout_seconds <= 0:
            raise ValueError("forge_unload_timeout_seconds must be positive")
        if forge_upstream:
            parsed_forge = urlsplit(forge_upstream)
            if parsed_forge.scheme not in {"http", "https"} or not parsed_forge.hostname:
                raise ValueError("forge_upstream must be an http or https URL")
        requested_root = root.resolve()
        self.root = (
            requested_root
            if requested_root.name.casefold() == "file_proxy"
            else requested_root / "File_Proxy"
        )
        self.registry = registry
        if isinstance(cloud_concurrency, bool) or not isinstance(cloud_concurrency, int) or cloud_concurrency < 1:
            raise ValueError("cloud_concurrency must be a positive integer")
        self.cloud_concurrency = cloud_concurrency
        self.codex_models = codex_models
        self._active_jobs: set[tuple[str, str]] = set()
        self._scan_guard = threading.RLock()
        self.ollama_upstream = "http://127.0.0.1:11434"
        self.comfyui_upstream = "http://127.0.0.1:8188"
        self.logger = logger or (lambda _message: None)
        self.job_logger: Callable[[str, str], None] | None = None
        self.sync_grace_seconds = sync_grace_seconds
        self.max_resource_streak = max_resource_streak
        self._resource_key: str | None = None
        self._resource_streak = 0
        self.forge_upstream = forge_upstream.rstrip("/") if forge_upstream else None
        self.forge_unload_timeout_seconds = forge_unload_timeout_seconds
        self.forge_cleanup_required = forge_cleanup_required
        self._not_ready_since: dict[Path, float] = {}
        self.ask_root = self.root / "Ask"
        self.running_root = self.root / "Running"
        self.answer_root = self.root / "Answer"
        self.control_root = self.root / "Control"
        self.local_runtime_dir = (local_runtime_dir or (Path.cwd() / ".ai-proxy-runtime")).resolve()
        self._filesystem_degraded: str | None = None
        queue_key = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()[:20]
        self.storage = StorageHealth(self.local_runtime_dir / ("storage-health-" + queue_key + ".json"), {"queue": self.root})
        self._storage_reconciled = False
        self._status_cache = []
        self._orphan_paths = set()
        for path in (self.local_runtime_dir / "executions").glob("*/*.json"):
            if not path.name.endswith((".result.json", ".submission.json")):
                self._orphan_paths.add(path)

    def ensure_layout(self) -> None:
        self.storage.require("queue")
        for path in (self.ask_root, self.running_root, self.answer_root, self.control_root):
            path.mkdir(parents=True, exist_ok=True)

    def _queue_ready(self) -> bool:
        try:
            self.ensure_layout()
            with self._scan_guard:
                if not self._storage_reconciled:
                    self.recover_interrupted()
                    self._storage_reconciled = True
            self._filesystem_degraded = None
            return True
        except OSError as exc:
            if self._filesystem_degraded != str(exc):
                self.logger(f"Filesystem queue paused; retrying automatically: {exc}")
            self._filesystem_degraded = str(exc)
            self._storage_reconciled = False
            self.storage.poll(force=True)
            return False

    def _execution_path(self, subscriber_id, job_id):
        return self.local_runtime_dir / "executions" / subscriber_id / (job_id + ".json")

    def _execution_alive(self, subscriber_id, job_id):
        from .worker_host import process_alive, execution_lock, save
        path = self._execution_path(subscriber_id, job_id)
        if not path.is_file():
            return False
        with execution_lock(path):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("phase") == "RESERVED":
                if process_alive(record.get("proxy")):
                    return True
                record["phase"] = "FENCED"
                save(path, record)
            return any(process_alive(record.get(key)) for key in ("host", "child"))

    def _orphan_count(self, *, cloud=False):
        """Surviving hosts retain their lane/capacity even when the old proxy died."""
        count = 0
        with self._scan_guard:
            for path in list(self._orphan_paths):
                identity = (path.parent.name, path.stem)
                if identity in self._active_jobs:
                    continue
                try:
                    if not self._execution_alive(*identity):
                        self._orphan_paths.discard(path)
                        continue
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if (record.get("backend") == "codex") == cloud:
                        count += 1
                except FileNotFoundError:
                    self._orphan_paths.discard(path)
                except (OSError, ValueError):
                    # The unreadable witness does not identify its lane, so
                    # reserve capacity conservatively in both local and cloud.
                    count += 1
        return count

    def lock(self) -> RuntimeLock:
        return RuntimeLock(self.local_runtime_dir / "proxy.lock")

    def _answer_path(self, subscriber_id: str, job_id: str) -> Path:
        return self.answer_root / subscriber_id / job_id

    def _answer_occupied(self, subscriber_id: str, job_id: str) -> bool:
        destination = self._answer_path(subscriber_id, job_id)
        if is_link_like(destination.parent):
            return True
        return destination.exists() or is_link_like(destination)

    def _move_to_answer(self, path: Path, subscriber_id: str, job_id: str) -> Path:
        destination = self._answer_path(subscriber_id, job_id)
        if is_link_like(self.answer_root) or is_link_like(destination.parent):
            raise AnswerCollision(f"answer directory is a link or reparse point: {destination.parent}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if is_link_like(self.answer_root) or is_link_like(destination.parent) or self._answer_occupied(subscriber_id, job_id):
            raise AnswerCollision(f"answer already exists: {destination}")
        _replace_with_retry(path, destination)
        execution = self._execution_path(subscriber_id, job_id)
        for metadata in (execution, execution.with_suffix(".result.json"), execution.with_suffix(".submission.json"),
                         self._local_outcome_path(subscriber_id, job_id)):
            try:
                metadata.unlink(missing_ok=True)
            except OSError as exc:
                self.logger(f"Unable to clean published execution metadata {metadata.name}: {exc}")
        return destination

    def _local_outcome_path(self, subscriber_id: str, job_id: str) -> Path:
        return self.local_runtime_dir / "worker-outcomes" / subscriber_id / f"{job_id}.json"

    def _persist_local_outcome(self, subscriber_id: str, job_id: str, payload: dict[str, Any]) -> None:
        path = self._local_outcome_path(subscriber_id, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, payload)

    def _restore_outcome(self, folder, payload):
        if payload.get("status") == "SUCCEEDED":
            try:
                validate_job_folder(folder, folder.parent.name, folder.name)
            except (InvalidJob, JobNotReady) as exc:
                payload.update(status="FAILED", error_type="INVALID_OUTPUT_STATE", error_message=str(exc),
                               retryable=False, recovery_class="", output_files=None)
            native_path = self._execution_path(folder.parent.name, folder.name).with_suffix(".result.json")
            if native_path.is_file() and payload["status"] == "SUCCEEDED":
                native = json.loads(native_path.read_text(encoding="utf-8"))
                output = native.get("expected_output", "")
                if output and Path(output).name == output and not (folder / output).is_file():
                    payload.update(status="FAILED", error_type="OUTPUT_UNAVAILABLE", output_files=None,
                                   error_message="Completed output is unavailable after interruption", retryable=True,
                                   recovery_class="EXECUTION_INTERRUPTED")
        if payload.get("output_files") is None:
            payload["output_files"] = _inventory_with_retry(folder)
        _write_json_atomic(folder / "proxy_result.json", payload)

    def _publish_corrupt_state(self, folder: Path, reason: str) -> bool:
        result = ProxyResult(
            protocol_version=1, job_id=folder.name, subscriber_id=folder.parent.name,
            worker="", status="FAILED", started_at=iso_utc(utc_now()),
            completed_at=iso_utc(utc_now()), duration_seconds=0.0, exit_code=None,
            error_type="RECOVERY_STATE_CORRUPT", error_message=reason, output_files=[],
        )
        try:
            result.output_files = _inventory_with_retry(folder)
        except InvalidJob:
            result.output_files = []
        _write_json_atomic(folder / "proxy_result.json", result.to_dict())
        try:
            self._move_to_answer(folder, folder.parent.name, folder.name)
        except AnswerCollision as exc:
            self.logger(f"Answer collision for {folder.parent.name}/{folder.name}; leaving Running job for operator action: {exc}")
            return False
        self.logger(f"Published {folder.parent.name}/{folder.name} as failed because recovery state was corrupt")
        return True

    def _invalid(self, path: Path, subscriber_id: str, job_id: str, message: str) -> Path | None:
        self.logger(f"Invalid job {subscriber_id}/{job_id}: {message}")
        started = utc_now()
        result = ProxyResult(
            protocol_version=1,
            job_id=job_id,
            subscriber_id=subscriber_id,
            worker="",
            status="INVALID",
            started_at=iso_utc(started),
            completed_at=iso_utc(utc_now()),
            duration_seconds=0.0,
            exit_code=None,
            error_type="INVALID_JOB",
            error_message=message,
            output_files=[],
        )
        if is_link_like(path):
            remove_link_like(path)
            path.mkdir()
        else:
            while True:
                linked = find_link_like(path)
                if linked is None:
                    break
                remove_link_like(linked)
        try:
            result.output_files = _inventory_with_retry(path)
        except InvalidJob:
            result.output_files = []
        _write_json_atomic(path / "proxy_result.json", result.to_dict())
        try:
            return self._move_to_answer(path, subscriber_id, job_id)
        except AnswerCollision as exc:
            self.logger(f"Answer collision for {subscriber_id}/{job_id}; leaving invalid job for operator action: {exc}")
            return None

    def recover_interrupted(self) -> int:
        self.storage.require("queue")
        recovered = 0
        for subscriber_dir in sorted(path for path in self.running_root.iterdir() if path.is_dir()):
            if is_link_like(subscriber_dir):
                continue
            for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir()):
                linked = find_link_like(job_dir)
                if linked is not None:
                    answer = self._invalid(job_dir, subscriber_dir.name, job_dir.name,
                                            "link or reparse point is not allowed in Running job")
                    recovered += int(answer is not None)
                    continue
                if (subscriber_dir.name, job_dir.name) in self._active_jobs:
                    continue
                try:
                    alive = self._execution_alive(subscriber_dir.name, job_dir.name)
                except (OSError, UnicodeError, ValueError) as exc:
                    self.logger(f"Recovery evidence for {subscriber_dir.name}/{job_dir.name} is unreadable; leaving it blocked: {exc}")
                    continue
                if alive:
                    continue
                native_result = self._execution_path(subscriber_dir.name, job_dir.name).with_suffix(".result.json")
                witness_path = self._execution_path(subscriber_dir.name, job_dir.name)
                local_outcome = self._local_outcome_path(subscriber_dir.name, job_dir.name)
                corrupt = None
                for state_path in (native_result, witness_path, local_outcome, job_dir / "proxy_result.json"):
                    if not state_path.is_file():
                        continue
                    try:
                        state_value = json.loads(state_path.read_text(encoding="utf-8"))
                        if not isinstance(state_value, dict):
                            raise ValueError("expected a JSON object")
                    except (OSError, UnicodeError, ValueError) as exc:
                        corrupt = f"{state_path.name} is unreadable: {exc}"
                        break
                if corrupt:
                    self._preserve_unsettled_backend(job_dir)
                    recovered += int(self._publish_corrupt_state(job_dir, corrupt))
                    continue
                if witness_path.is_file() and not self._local_outcome_path(subscriber_dir.name, job_dir.name).is_file():
                    witness = json.loads(witness_path.read_text(encoding="utf-8"))
                    if witness.get("phase") == "EXITED":
                        code = witness.get("exit_code")
                        payload = {"protocol_version": 1, "job_id": job_dir.name, "subscriber_id": subscriber_dir.name,
                            "worker": "", "status": "SUCCEEDED" if code == 0 else "FAILED",
                            "started_at": iso_utc(utc_now()), "completed_at": iso_utc(utc_now()),
                            "duration_seconds": 0, "exit_code": code, "output_files": None,
                            "error_type": None if code == 0 else "WORKER_EXIT",
                            "error_message": "" if code == 0 else f"Worker exited with code {code}."}
                        if native_result.is_file():
                            native = json.loads(native_result.read_text(encoding="utf-8"))
                            if native.get("retryable"):
                                payload.update(recovery_class=native["recovery_class"], retryable=True)
                            if native.get("status") == "SUCCESS":
                                payload.update(status="SUCCEEDED", error_type=None, error_message="")
                            elif code not in {0, 124} and native.get("error_type") in {"URLError", "ConnectionError", "OSError"}:
                                payload.update(recovery_class="EXECUTION_INTERRUPTED", retryable=True)
                        elif code not in {0, 124}:
                            payload.update(recovery_class="EXECUTION_INTERRUPTED", retryable=True)
                        self._persist_local_outcome(subscriber_dir.name, job_dir.name, payload)
                if native_result.is_file():
                    native = json.loads(native_result.read_text(encoding="utf-8"))
                    if not (job_dir / "answer_manifest.json").is_file():
                        _write_json_atomic(job_dir / "answer_manifest.json", native)
                    output = native.get("expected_output", "")
                    if (native.get("status") == "SUCCESS" and output and Path(output).name == output
                            and (job_dir / output).is_file()
                            and not self._local_outcome_path(subscriber_dir.name, job_dir.name).is_file()):
                        self._persist_local_outcome(subscriber_dir.name, job_dir.name, {
                            "protocol_version": 1, "job_id": job_dir.name, "subscriber_id": subscriber_dir.name,
                            "worker": "", "status": "SUCCEEDED", "started_at": native["started_at"],
                            "completed_at": native["completed_at"], "duration_seconds": 0, "exit_code": 0,
                            "output_files": None})
                self._preserve_unsettled_backend(job_dir)
                if local_outcome.is_file() and not (job_dir / "proxy_result.json").is_file():
                    try:
                        payload = json.loads(local_outcome.read_text(encoding="utf-8"))
                        if not isinstance(payload, dict):
                            raise ValueError("worker outcome must be a JSON object")
                    except (OSError, UnicodeError, ValueError) as exc:
                        recovered += int(self._publish_corrupt_state(job_dir, f"{local_outcome.name} is unreadable: {exc}"))
                        continue
                    self._restore_outcome(job_dir, payload)
                if (job_dir / "proxy_result.json").is_file():
                    try:
                        self._move_to_answer(job_dir, subscriber_dir.name, job_dir.name)
                    except AnswerCollision as exc:
                        self.logger(f"Answer collision for {subscriber_dir.name}/{job_dir.name}; leaving Running job for operator action: {exc}")
                        continue
                    self.logger(f"Published previously completed job {subscriber_dir.name}/{job_dir.name}")
                    recovered += 1
                    continue
                started = utc_now()
                worker = ""
                try:
                    manifest = validate_job_folder(job_dir, subscriber_dir.name, job_dir.name)
                    worker = manifest.worker
                except (InvalidJob, JobNotReady):
                    pass
                result = ProxyResult(
                    protocol_version=1,
                    job_id=job_dir.name,
                    subscriber_id=subscriber_dir.name,
                    worker=worker,
                    status="FAILED",
                    started_at=iso_utc(started),
                    completed_at=iso_utc(utc_now()),
                    duration_seconds=0.0,
                    exit_code=None,
                    error_type="PROXY_INTERRUPTED",
                    error_message="Proxy stopped while this job was running; the job was not retried.",
                    output_files=_inventory_with_retry(job_dir),
                )
                _write_json_atomic(job_dir / "proxy_result.json", {**result.to_dict(), "recovery_class": "EXECUTION_INTERRUPTED", "retryable": True})
                try:
                    self._move_to_answer(job_dir, subscriber_dir.name, job_dir.name)
                except AnswerCollision as exc:
                    self.logger(f"Answer collision for {subscriber_dir.name}/{job_dir.name}; leaving Running job for operator action: {exc}")
                    continue
                self.logger(f"Recovered interrupted job {subscriber_dir.name}/{job_dir.name} as FAILED")
                recovered += 1
        return recovered

    def _scan(self) -> tuple[list[QueueJob], list[tuple[Path, str, str, str]]]:
        self.storage.require("queue")
        jobs: list[QueueJob] = []
        invalid: list[tuple[Path, str, str, str]] = []
        for subscriber_dir in sorted(path for path in self.ask_root.iterdir() if path.is_dir()):
            if subscriber_dir.name.startswith("."):
                continue
            if is_link_like(subscriber_dir):
                continue
            for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir()):
                if job_dir.name.startswith("."):
                    continue
                if self._answer_occupied(subscriber_dir.name, job_dir.name):
                    continue
                try:
                    manifest = validate_job_folder(job_dir, subscriber_dir.name, job_dir.name)
                    if self.registry.worker(manifest.subscriber_id, manifest.worker) is None:
                        raise InvalidJob(f"unknown worker {manifest.worker!r} for subscriber {manifest.subscriber_id!r}")
                    jobs.append(QueueJob(job_dir, manifest))
                    self._not_ready_since.pop(job_dir, None)
                except JobNotReady as exc:
                    now = time.monotonic()
                    first_seen = self._not_ready_since.setdefault(job_dir, now)
                    if now - first_seen >= self.sync_grace_seconds:
                        invalid.append(
                            (
                                job_dir,
                                subscriber_dir.name,
                                job_dir.name,
                                f"sync did not complete within {self.sync_grace_seconds:g}s: {exc}",
                            )
                        )
                    elif now == first_seen:
                        self.logger(
                            f"Waiting for Dropbox sync: "
                            f"{subscriber_dir.name}/{job_dir.name}: {exc}"
                        )
                except InvalidJob as exc:
                    self._not_ready_since.pop(job_dir, None)
                    invalid.append((job_dir, subscriber_dir.name, job_dir.name, str(exc)))
        jobs.sort(key=lambda item: (
            -item.manifest.priority,
            item.manifest.created_at,
            item.manifest.subscriber_id,
            item.manifest.job_id,
        ))
        return jobs, invalid

    def _select_file_job(
        self,
        *, include_cloud: bool = False,
    ) -> QueueJob | tuple[Path, str, str, str] | None:
        if not self._queue_ready():
            return None
        while self._publish_completed_running():
            pass
        jobs, invalid = self._scan()
        if self._orphan_count():
            jobs = [job for job in jobs if self._file_job_backend(job) == "codex"]
        if self._orphan_count(cloud=True) >= self.cloud_concurrency:
            jobs = [job for job in jobs if self._file_job_backend(job) != "codex"]
        if not include_cloud:
            jobs = [job for job in jobs if self._file_job_backend(job) != "codex"]
        if invalid:
            return invalid[0]
        if not jobs:
            return None
        return self._choose_resource_job(jobs, self._resource_key, self._resource_streak)

    def _publish_completed_running(self) -> bool:
        if not self.running_root.is_dir():
            return False
        for subscriber_dir in sorted(path for path in self.running_root.iterdir() if path.is_dir()):
            if is_link_like(subscriber_dir):
                continue
            for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir()):
                linked = find_link_like(job_dir)
                if linked is not None:
                    answer = self._invalid(job_dir, subscriber_dir.name, job_dir.name,
                                           "link or reparse point is not allowed in Running job")
                    return answer is not None
                if (subscriber_dir.name, job_dir.name) in self._active_jobs:
                    continue
                try:
                    if self._execution_alive(subscriber_dir.name, job_dir.name):
                        continue
                except (OSError, UnicodeError, ValueError) as exc:
                    self.logger(f"Recovery evidence for {subscriber_dir.name}/{job_dir.name} is unreadable; leaving it blocked: {exc}")
                    continue
                local_outcome = self._local_outcome_path(subscriber_dir.name, job_dir.name)
                if local_outcome.is_file() and not (job_dir / "proxy_result.json").is_file():
                    try:
                        payload = json.loads(local_outcome.read_text(encoding="utf-8"))
                        if not isinstance(payload, dict):
                            raise ValueError("worker outcome must be a JSON object")
                    except (OSError, UnicodeError, ValueError) as exc:
                        self._preserve_unsettled_backend(job_dir)
                        return self._publish_corrupt_state(job_dir, f"{local_outcome.name} is unreadable: {exc}")
                    try:
                        self._restore_outcome(job_dir, payload)
                    except (OSError, UnicodeError, ValueError) as exc:
                        self._preserve_unsettled_backend(job_dir)
                        return self._publish_corrupt_state(job_dir, f"Recovery data is unreadable: {exc}")
                if not (job_dir / "proxy_result.json").is_file():
                    return self.recover_interrupted() > 0
                if (job_dir / "proxy_result.json").is_file():
                    try:
                        self._move_to_answer(job_dir, subscriber_dir.name, job_dir.name)
                    except AnswerCollision as exc:
                        self.logger(f"Answer collision for {subscriber_dir.name}/{job_dir.name}; leaving Running job for operator action: {exc}")
                        return False
                    self.logger(f"Published completed outcome {subscriber_dir.name}/{job_dir.name}")
                    return True
        return False

    def _choose_resource_job(
        self,
        jobs: list[QueueJob],
        resource_key: str | None,
        resource_streak: int,
    ) -> QueueJob:
        first_backend = self._file_job_backend(jobs[0])
        eligible: list[QueueJob] = []
        for job in jobs:
            if eligible and self._file_job_backend(job) != first_backend:
                break
            eligible.append(job)
        highest_priority = max(job.manifest.priority for job in eligible)
        eligible = [job for job in eligible if job.manifest.priority == highest_priority]
        if resource_key is None:
            return eligible[0]
        if resource_streak < self.max_resource_streak:
            matching = next((job for job in eligible if job.manifest.resource_key == resource_key), None)
            if matching is not None:
                return matching
        different = next((job for job in eligible if job.manifest.resource_key != resource_key), None)
        return different or eligible[0]

    def _file_job_backend(self, job: QueueJob) -> str | None:
        registration = self.registry.worker(job.manifest.subscriber_id, job.manifest.worker)
        if registration and registration.backend:
            return registration.backend
        if job.manifest.resource_key and ":" in job.manifest.resource_key:
            prefix = job.manifest.resource_key.split(":", 1)[0]
            if prefix in {"ollama", "comfyui"}:
                return prefix
        return None

    def _record_resource_job(self, manifest: JobManifest) -> None:
        self._record_resource_key(manifest.resource_key)

    def _prepare_resource(self, resource_key: str | None) -> None:
        if (
            not self.forge_upstream
            or not resource_key
            or not resource_key.startswith("ollama:")
            or self._resource_key is not None and self._resource_key.startswith("ollama:")
        ):
            return
        url = f"{self.forge_upstream}/sdapi/v1/unload-checkpoint"
        request = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.forge_unload_timeout_seconds) as response:
                status = int(response.status)
            if 200 <= status < 300:
                self.logger(f"Released Forge checkpoint before {resource_key}")
                return
            raise RuntimeError(f"Forge cleanup returned HTTP {status}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if self.forge_cleanup_required:
                self.logger(f"Forge cleanup failed before {resource_key}: {exc}")
                raise RuntimeError("Forge cleanup failed; unsafe resource transition was blocked") from exc

    def _record_resource_key(self, resource_key: str | None) -> None:
        if resource_key is None:
            self._resource_key = None
            self._resource_streak = 0
        elif resource_key == self._resource_key:
            self._resource_streak += 1
        else:
            self._resource_key = resource_key
            self._resource_streak = 1

    def _next_resource_hint(self) -> tuple[bool, str]:
        jobs, invalid = self._scan()
        jobs = [job for job in jobs if self._file_job_backend(job) != "codex"]
        if invalid:
            return True, ""
        if not jobs:
            return False, ""
        selected = self._choose_resource_job(jobs, self._resource_key, self._resource_streak)
        return True, selected.manifest.resource_key or ""

    def _process_file_job(
        self,
        selected: QueueJob | tuple[Path, str, str, str],
    ) -> Path | None:
        if isinstance(selected, tuple):
            return self._invalid(*selected)
        if self._file_job_backend(selected) != "codex" and self._local_admission_blocked():
            return None
        identity = (selected.manifest.subscriber_id, selected.manifest.job_id)
        with self._scan_guard:
            self._active_jobs.add(identity)
        try:
            return self._execute_file_job(selected)
        finally:
            with self._scan_guard:
                self._active_jobs.discard(identity)

    def _execute_file_job(self, selected: QueueJob) -> Path | None:
        self.storage.require("queue")
        if isinstance(selected, tuple):
            return self._invalid(*selected)
        queued = selected
        manifest = queued.manifest
        resource_tag = f"[{manifest.resource_key}] " if manifest.resource_key else ""
        backend = self._file_job_backend(queued)

        def log_job(message: str) -> None:
            if self.job_logger is None:
                self.logger(message)
            else:
                self.job_logger(backend or manifest.worker, message)

        log_job(
            f"{resource_tag}Starting {manifest.subscriber_id}/{manifest.job_id} "
            f"with worker {manifest.worker}"
        )
        registration = self.registry.worker(manifest.subscriber_id, manifest.worker)
        assert registration is not None
        running = self.running_root / manifest.subscriber_id / manifest.job_id
        running.parent.mkdir(parents=True, exist_ok=True)
        _replace_with_retry(queued.path, running)
        transition_error: str | None = None
        cloud = registration.backend == "codex"
        next_job_present, next_resource_key = False, ""
        if not cloud:
            try:
                self._prepare_resource(manifest.resource_key)
            except RuntimeError as exc:
                transition_error = str(exc)
            self._record_resource_job(manifest)
            next_job_present, next_resource_key = self._next_resource_hint()
        started = utc_now()
        status = "FAILED" if transition_error else "SUCCEEDED"
        exit_code: int | None = None
        error_type: str | None = "RESOURCE_TRANSITION" if transition_error else None
        error_message: str | None = transition_error
        stdout = ""
        stderr = ""
        try:
            if transition_error:
                raise BackendUnavailable(transition_error)
            import sys
            execution_path = self._execution_path(manifest.subscriber_id, manifest.job_id)
            execution_path.parent.mkdir(parents=True, exist_ok=True)
            for stale in (execution_path.with_suffix(".result.json"), execution_path.with_suffix(".submission.json"),
                          self._local_outcome_path(manifest.subscriber_id, manifest.job_id)):
                stale.unlink(missing_ok=True)
            from .worker_host import process_identity
            _write_json_atomic(execution_path, {"version": 1, "phase": "RESERVED", "proxy": process_identity(os.getpid()),
                                               "backend": self._file_job_backend(selected)})
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("worker_host.py")), "--journal", str(execution_path),
                 "--timeout", str(registration.timeout_seconds), "--", *registration.command, "--job-dir", str(running)],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=registration.timeout_seconds + 30,
                check=False,
                cwd=registration.working_directory,
                env={
                    **os.environ,
                    "AI_PROXY_OLLAMA_UPSTREAM": self.ollama_upstream,
                    "AI_PROXY_COMFYUI_UPSTREAM": self.comfyui_upstream,
                    "AI_PROXY_CURRENT_RESOURCE_KEY": manifest.resource_key or "",
                    "AI_PROXY_NEXT_JOB_PRESENT": "1" if next_job_present else "0",
                    "AI_PROXY_NEXT_RESOURCE_KEY": next_resource_key,
                    "AI_PROXY_EXECUTION_STATE": str(execution_path),
                    "AI_PROXY_RUNTIME_DIR": str(self.local_runtime_dir),
                },
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            if completed.returncode != 0:
                status = "FAILED"
                error_type = "WORKER_EXIT"
                error_message = f"Worker exited with code {completed.returncode}."
                if stderr.strip():
                    error_message += " " + stderr.strip()[-2000:]
        except BackendUnavailable:
            pass
        except subprocess.TimeoutExpired as exc:
            status = "FAILED"
            error_type = "WORKER_TIMEOUT"
            error_message = f"Worker exceeded timeout of {registration.timeout_seconds:g} seconds."
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        except OSError as exc:
            status = "FAILED"
            error_type = "WORKER_LAUNCH"
            error_message = str(exc)
        if running.is_dir():
            try:
                validate_job_folder(running, manifest.subscriber_id, manifest.job_id)
            except (InvalidJob, JobNotReady) as exc:
                status = "FAILED"
                error_type = "INVALID_OUTPUT_STATE"
                error_message = str(exc)
        completed_at = utc_now()
        self._preserve_unsettled_backend(running)
        # Persist execution outcome before touching a potentially disconnected queue.
        # A failed publication must not turn a successful execution into a rerun.
        result = ProxyResult(
            protocol_version=1,
            job_id=manifest.job_id,
            subscriber_id=manifest.subscriber_id,
            worker=manifest.worker,
            status=status,
            started_at=iso_utc(started),
            completed_at=iso_utc(completed_at),
            duration_seconds=round((completed_at - started).total_seconds(), 6),
            exit_code=exit_code,
            error_type=error_type,
            error_message=error_message,
            stdout=stdout,
            stderr=stderr,
            output_files=None,
        )
        payload = result.to_dict()
        worker_result = self._execution_path(manifest.subscriber_id, manifest.job_id).with_suffix(".result.json")
        if worker_result.is_file():
            native = json.loads(worker_result.read_text(encoding="utf-8"))
            if native.get("retryable"):
                payload.update(recovery_class=native["recovery_class"], retryable=True)
            if native.get("status") == "SUCCESS" and (error_type == "WORKER_EXIT" or not self.storage.poll(force=True)["available"]):
                payload.update(status="SUCCEEDED", error_type=None, error_message="", exit_code=0)
        if (payload["status"] == "FAILED" and error_type in {"WORKER_LAUNCH", "INVALID_OUTPUT_STATE", "WORKER_EXIT"}
                and not worker_result.is_file() and not self.storage.poll(force=True)["available"]):
            payload.update(recovery_class="STORAGE_UNAVAILABLE", retryable=True)
        self._persist_local_outcome(manifest.subscriber_id, manifest.job_id, payload)
        self.storage.require("queue")
        if not running.is_dir():
            raise StorageUnavailable(f"Execution folder is unavailable: {running}")
        payload["output_files"] = _inventory_with_retry(running)
        self._persist_local_outcome(manifest.subscriber_id, manifest.job_id, payload)
        _write_json_atomic(running / "proxy_result.json", payload)
        try:
            answer = self._move_to_answer(running, manifest.subscriber_id, manifest.job_id)
        except AnswerCollision as exc:
            log_job(
                f"Answer collision for {manifest.subscriber_id}/{manifest.job_id}; "
                f"attempt remains in Running for operator action: {exc}"
            )
            return None
        log_job(
            f"Finished {manifest.subscriber_id}/{manifest.job_id}: "
            f"{status} in {result.duration_seconds:.3f}s"
        )
        return answer

    def process_once(self) -> Path | None:
        self._clear_unsubmitted_guard()
        if self._orphan_count():
            return None
        if self._local_admission_blocked():
            return None
        selected = self._select_file_job()
        self._clear_unsubmitted_guard()
        if selected is None or self._local_admission_blocked():
            return None
        return self._process_file_job(selected)

    def _preserve_unsettled_backend(self, folder: Path) -> None:
        guard = self.local_runtime_dir / "unsettled_comfyui.json"
        local = self._execution_path(folder.parent.name, folder.name).with_suffix(".submission.json")
        if local.is_file():
            try:
                submission = json.loads(local.read_text(encoding="utf-8"))
                if not isinstance(submission, dict):
                    raise ValueError("submission sidecar must contain a JSON object")
            except (OSError, UnicodeError, ValueError) as exc:
                if not guard.exists():
                    _write_json_atomic(guard, {"job_id": folder.name,
                                               "reason": f"Local ComfyUI submission evidence is unreadable: {exc}"})
                return
            if submission.get("settled") is not True and not guard.is_file():
                _write_json_atomic(guard, submission)
            return
        if guard.is_file():
            return
        path = folder / "comfyui_submission.json"
        if path.is_file():
            try:
                submission = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(submission, dict):
                    raise ValueError("saved submission must contain a JSON object")
                if submission.get("settled") is not True:
                    _write_json_atomic(self.local_runtime_dir / "unsettled_comfyui.json", {
                        "job_id": folder.name, "prompt_id": submission.get("prompt_id"),
                        "reason": "ComfyUI submission is unresolved. Reconcile/stop the backend before clearing this local guard."})
            except (OSError, ValueError) as exc:
                self.logger(f"Unable to reconcile saved ComfyUI submission: {exc}")

    def _clear_unsubmitted_guard(self):
        guard = self.local_runtime_dir / "unsettled_comfyui.json"
        try:
            if is_link_like(guard):
                self.logger("ComfyUI guard is a link or reparse point; local execution remains blocked")
                return False
            if guard.is_file():
                value = json.loads(guard.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("phase") == "PREPARED":
                    guard.replace(guard.with_name(f"unsettled_comfyui.unsubmitted-{time.time_ns()}.json"))
                    return True
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.logger(f"ComfyUI guard is unreadable; local execution remains blocked: {exc}")
        return False

    def _local_admission_blocked(self) -> bool:
        state, _value = self._guard_state()
        return state != "clear"

    def _guard_state(self) -> tuple[str, dict[str, Any] | None]:
        guard = self.local_runtime_dir / "unsettled_comfyui.json"
        try:
            guard.lstat()
        except FileNotFoundError:
            return "clear", None
        except OSError as exc:
            self.logger(f"ComfyUI guard cannot be inspected; local execution remains blocked: {exc}")
            return "unreadable", None
        try:
            if is_link_like(guard):
                raise ValueError("guard cannot be a link or reparse point")
            if not guard.is_file():
                raise ValueError("guard path is not a regular file")
            value = json.loads(guard.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("guard must contain a JSON object")
            return "guarded", value
        except (OSError, UnicodeError, ValueError) as exc:
            self.logger(f"ComfyUI guard is unreadable; local execution remains blocked: {exc}")
            return "unreadable", None

    def _guard_can_managed_recover(self) -> bool:
        state, value = self._guard_state()
        return (state == "guarded" and value is not None
                and value.get("phase") in {"DISPATCHING", "ACCEPTED"}
                and value.get("settled") is not True)

    async def _reconcile_unsettled_comfyui(self, backend_manager: BackendManager) -> None:
        """End ambiguous managed work before allowing another local attempt."""
        # This stops only our owned process and refuses a reachable external one.
        # A failed/interrupted file attempt already has its terminal outcome;
        # recovery releases admission, never replays or rewrites that attempt.
        await backend_manager.stop_comfyui()
        guard = self.local_runtime_dir / "unsettled_comfyui.json"
        archive = guard.with_name(f"unsettled_comfyui.recovered-{time.time_ns()}.json")
        await asyncio.to_thread(_replace_with_retry, guard, archive)
        self.logger(f"Recovered unresolved ComfyUI submission after verified backend stop; saved {archive.name}. Local execution resumes.")

    @contextmanager
    def cloud_dispatch(self, poll_seconds: float = 1.0):
        """Independent trusted Codex lane; stopping waits for admitted workers."""
        stop = threading.Event()
        def dispatch():
            with ThreadPoolExecutor(max_workers=self.cloud_concurrency, thread_name_prefix="proxy-cloud") as pool:
                pending = {}
                last_blocked = None
                while not stop.is_set():
                    unsettled = self._local_admission_blocked()
                    blocked = unsettled or bool(self._orphan_count())
                    if blocked != last_blocked:
                        try:
                            _write_json_atomic(self.control_root / "execution_status.json", {
                                "local_execution_blocked": blocked,
                                "reason": ("ComfyUI submission is unresolved; reconcile the proxy's saved prompt before local execution resumes." if unsettled else
                                           "A surviving worker still owns local execution; waiting for it to finish." if blocked else ""),
                                "cloud_concurrency": self.cloud_concurrency})
                            last_blocked = blocked
                        except OSError as exc:
                            self.logger(f"Unable to publish execution status: {exc}")
                    for identity, future in list(pending.items()):
                        if future.done():
                            try:
                                future.result()
                            except Exception as exc:
                                self.logger(f"Cloud worker {identity}: {exc}")
                            del pending[identity]
                    try:
                        if not self._queue_ready():
                            stop.wait(max(0.01, poll_seconds))
                            continue
                        jobs, _ = self._scan()
                        for job in jobs:
                            identity = (job.manifest.subscriber_id, job.manifest.job_id)
                            if len(pending) + self._orphan_count(cloud=True) >= self.cloud_concurrency:
                                break
                            if self._file_job_backend(job) != "codex" or identity in pending:
                                continue
                            pending[identity] = pool.submit(self._process_file_job, job)
                    except OSError as exc:
                        self.logger(f"Cloud queue unavailable: {exc}")
                    stop.wait(max(0.01, poll_seconds))
                for future in pending.values():
                    try:
                        future.result()
                    except Exception as exc:
                        self.logger(f"Cloud shutdown: {exc}")
        thread = threading.Thread(target=dispatch, name="proxy-cloud-dispatch", daemon=True)
        thread.start()
        def refresh_catalog():
            from .model_catalog import ModelCatalog
            catalog = ModelCatalog(self.control_root / "model_catalog.json", self.ollama_upstream, self.comfyui_upstream, self.codex_models)
            while not stop.is_set():
                try:
                    catalog.refresh()
                except OSError as exc:
                    self.logger(f"Model catalog unavailable: {exc}")
                for _ in range(300):
                    if stop.wait(1):
                        return
                    request = self.control_root / "refresh-models.json"
                    try:
                        if request.is_file():
                            request.unlink(missing_ok=True)
                            break
                    except OSError as exc:
                        self.logger(f"Unable to clear model refresh request; retrying: {exc}")
        native = any("file_proxy.ai_worker" in registration.command
                     for workers in self.registry.subscribers.values() for registration in workers.values())
        catalog_thread = threading.Thread(target=refresh_catalog, name="proxy-model-catalog", daemon=True) if native else None
        if catalog_thread:
            catalog_thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
            if catalog_thread:
                catalog_thread.join(timeout=5)

    async def run_scheduler(
        self,
        http_queue: HttpQueue,
        poll_seconds: float = 1.0,
        continuation_grace_seconds: float = 2.0,
        backend_manager: BackendManager | None = None,
        ollama_upstream: str | None = None,
    ) -> None:
        cloud = self.cloud_dispatch(poll_seconds)
        cloud.__enter__()
        try:
            await self._run_local_scheduler(http_queue, poll_seconds, continuation_grace_seconds,
                                            backend_manager, ollama_upstream)
        finally:
            await asyncio.to_thread(cloud.__exit__, None, None, None)

    async def _run_local_scheduler(
        self, http_queue: HttpQueue, poll_seconds: float, continuation_grace_seconds: float,
        backend_manager: BackendManager | None, ollama_upstream: str | None,
    ) -> None:
        last_http_completed: float | None = None
        deferred_selected: QueueJob | tuple[Path, str, str, str] | None = None
        last_recovery_error: str | None = None
        loop = asyncio.get_running_loop()
        while True:
            self._clear_unsubmitted_guard()
            if self._local_admission_blocked():
                if backend_manager and backend_manager.config.enabled and self._guard_can_managed_recover():
                    try:
                        await self._reconcile_unsettled_comfyui(backend_manager)
                        last_recovery_error = None
                        continue
                    except (BackendUnavailable, OSError) as exc:
                        if str(exc) != last_recovery_error:
                            self.logger(f"ComfyUI recovery is held: {exc}")
                            last_recovery_error = str(exc)
                await asyncio.sleep(max(0.01, poll_seconds))
                continue
            if self._orphan_count():
                await asyncio.sleep(max(0.01, poll_seconds))
                continue
            selected = deferred_selected
            deferred_selected = None
            http_candidate = http_queue.peek()
            if http_candidate is not None and selected is None:
                try:
                    selected = await asyncio.to_thread(self._select_file_job)
                    self._filesystem_degraded = None
                except OSError as exc:
                    self._filesystem_degraded = str(exc)
                    self.logger(f"Filesystem queue unavailable; API listeners remain active: {exc}")
            if http_candidate is not None and selected is not None:
                if isinstance(selected, QueueJob):
                    file_backend = self._file_job_backend(selected)
                    http_backend = HttpQueue._backend(http_candidate)
                    file_is_older = selected.manifest.created_at.timestamp() <= http_candidate.enqueued_wall_time
                    if not (file_is_older and file_backend and http_backend and file_backend != http_backend):
                        deferred_selected = selected
                        selected = None
                else:
                    deferred_selected = selected
                    selected = None
            if self._local_admission_blocked():
                await asyncio.sleep(max(0.01, poll_seconds))
                continue
            http_job = None if selected is not None else http_queue.pop()
            if http_job is not None:
                try:
                    if backend_manager:
                        await backend_manager.prepare_for(http_job.backend, ollama_upstream=ollama_upstream, job_id=http_job.request_id)
                    await asyncio.to_thread(self._prepare_resource, http_job.resource_key)
                    self._record_resource_key(http_job.resource_key)
                except (BackendUnavailable, RuntimeError) as exc:
                    http_job.start_error = str(exc)
                if not http_job.started.done():
                    http_job.started.set_result(None)
                await http_job.finished
                if backend_manager:
                    await backend_manager.released(http_job.backend)
                last_http_completed = loop.time()
                continue

            if last_http_completed is not None:
                remaining = continuation_grace_seconds - (loop.time() - last_http_completed)
                if remaining > 0:
                    deferred_selected = selected
                    await http_queue.wait(remaining)
                    continue
                last_http_completed = None

            if selected is None:
                try:
                    selected = await asyncio.to_thread(self._select_file_job)
                    self._filesystem_degraded = None
                except OSError as exc:
                    self._filesystem_degraded = str(exc)
                    self.logger(f"Filesystem queue unavailable; API listeners remain active: {exc}")
                    await http_queue.wait(poll_seconds)
                    continue
            if http_queue.has_waiting() and selected is None:
                continue
            if self._local_admission_blocked():
                await asyncio.sleep(max(0.01, poll_seconds))
                continue
            if selected is None:
                await http_queue.wait(poll_seconds)
                continue
            file_backend = None
            if isinstance(selected, QueueJob):
                registration = self.registry.worker(selected.manifest.subscriber_id, selected.manifest.worker)
                file_backend = registration.backend if registration else None
                if file_backend is None and selected.manifest.resource_key:
                    prefix = selected.manifest.resource_key.split(":", 1)[0]
                    file_backend = prefix if prefix in {"ollama", "comfyui"} else None
            if isinstance(selected, QueueJob) and backend_manager and backend_manager.config.enabled and file_backend is None:
                self.logger("Filesystem worker is held: managed mode requires a trusted backend declaration")
                await http_queue.wait(poll_seconds)
                continue
            if backend_manager and isinstance(selected, QueueJob):
                try:
                    file_job_id = selected.manifest.job_id if isinstance(selected, QueueJob) else None
                    await backend_manager.prepare_for(file_backend, ollama_upstream=ollama_upstream, job_id=file_job_id)
                except BackendUnavailable as exc:
                    self.logger(f"Filesystem worker is held: {exc}")
                    await http_queue.wait(poll_seconds)
                    continue
            worker_task = asyncio.create_task(asyncio.to_thread(self._process_file_job, selected))
            try:
                await asyncio.shield(worker_task)
            except OSError as exc:
                self._filesystem_degraded = str(exc)
                self.logger(f"Filesystem processing degraded: {exc}")
            except asyncio.CancelledError:
                await worker_task
                raise
            finally:
                if backend_manager and isinstance(selected, QueueJob):
                    await backend_manager.released(file_backend)

    def run(self, poll_seconds: float = 1.0) -> None:
        self.logger(f"Queue root: {self.root}")
        with self.lock():
            self.logger(f"Lock acquired: {self.local_runtime_dir / 'proxy.lock'}")
            self._queue_ready()
            with self.cloud_dispatch(poll_seconds):
                self.logger(f"Ready; polling every {poll_seconds:g}s. Press Ctrl-C to stop.")
                idle = False
                while True:
                    try:
                        outcome = self.process_once()
                    except OSError:
                        self._storage_reconciled = False
                        outcome = None
                    if outcome is None:
                        if not idle:
                            self.logger("Queue is idle")
                            idle = True
                        time.sleep(poll_seconds)
                    else:
                        idle = False

    def once(self) -> Path | None:
        self.ensure_layout()
        with self.lock():
            self.recover_interrupted()
            selected = self._select_file_job(include_cloud=True)
            self._clear_unsubmitted_guard()
            if selected is None:
                return None
            if self._file_job_backend(selected) != "codex" and self._local_admission_blocked():
                return None
            return self._process_file_job(selected)

    def status(self, subscriber: str | None = None, job: str | None = None) -> dict[str, Any]:
        try:
            return self._available_status(subscriber, job)
        except OSError as exc:
            self.storage.poll(force=True)
            return {"counts": None, "jobs": [dict(row, status="PAUSED_STORAGE") for row in self._status_cache],
                    "degraded": str(exc), "storage": self.storage.status()}

    def _available_status(self, subscriber: str | None = None, job: str | None = None) -> dict[str, Any]:
        try:
            self.ensure_layout()
        except OSError as exc:
            return {"counts": None, "jobs": [dict(row, status="PAUSED_STORAGE") for row in self._status_cache],
                    "degraded": str(exc), "storage": self.storage.status()}
        counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "invalid": 0, "blocked": 0}
        items: list[dict[str, Any]] = []
        for state, root in (("queued", self.ask_root), ("running", self.running_root), ("answer", self.answer_root)):
            for subscriber_dir in sorted(path for path in root.iterdir() if path.is_dir()):
                if is_link_like(subscriber_dir):
                    continue
                if subscriber and subscriber_dir.name != subscriber:
                    continue
                for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir() and not path.name.startswith(".")):
                    if job and job_dir.name != job:
                        continue
                    item_state = state
                    item: dict[str, Any] = {
                        "subscriber_id": subscriber_dir.name, "job_id": job_dir.name, "status": item_state,
                        "display_state": "queued" if state == "queued" else "starting backend" if state == "running" else item_state,
                        "diagnostic": "", "worker": "", "trusted_worker_backend": None,
                    }
                    if is_link_like(job_dir):
                        item.update(status="blocked", display_state="blocked",
                                    diagnostic="Job path is a link or reparse point")
                        counts["blocked"] += 1
                        items.append(item)
                        continue
                    if state in {"queued", "running"}:
                        if self._answer_occupied(subscriber_dir.name, job_dir.name):
                            item.update(status="blocked", display_state="blocked",
                                        diagnostic="Answer path already exists; resolve the duplicate job ID before publication")
                            counts["blocked"] += 1
                            items.append(item)
                            continue
                        try:
                            manifest_data = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                            manifest = JobManifest.from_dict(manifest_data)
                            if manifest.subscriber_id != subscriber_dir.name or manifest.job_id != job_dir.name:
                                raise InvalidJob("job identifiers do not match queue folder")
                            registration = self.registry.worker(manifest.subscriber_id, manifest.worker)
                            item["worker"] = manifest.worker
                            if registration is None:
                                item["diagnostic"] = f"Unknown trusted worker: {manifest.worker}"
                            else:
                                item["trusted_worker_backend"] = registration.backend
                            validate_job_folder(job_dir, subscriber_dir.name, job_dir.name)
                        except (InvalidJob, JobNotReady) as exc:
                            item["diagnostic"] = str(exc)
                        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                            item["diagnostic"] = f"Invalid job manifest: {exc}"
                    if state == "answer":
                        try:
                            result = json.loads((job_dir / "proxy_result.json").read_text(encoding="utf-8"))
                            item_state = str(result.get("status", "FAILED")).lower()
                            item.update(status=item_state, worker=str(result.get("worker") or ""),
                                         diagnostic=str(result.get("error_message") or ""))
                            if item_state == "invalid":
                                item["display_state"] = "held"
                        except (OSError, json.JSONDecodeError):
                            item_state = "failed"
                    counts[item_state] += 1
                    items.append(item)
        guard_state, _guard_value = self._guard_state()
        guard_blocked = guard_state != "clear"
        blocked = guard_blocked or bool(self._orphan_count())
        self._status_cache = items
        return {"counts": counts, "jobs": items, "degraded": self._filesystem_degraded,
                "storage": self.storage.status(),
                "local_execution_blocked": blocked,
                "local_execution_block_reason": ("ComfyUI guard is unreadable; operator recovery is required"
                                                  if guard_state == "unreadable" else
                                                  "ComfyUI submission is unresolved" if guard_blocked else ""),
                "cloud_concurrency": self.cloud_concurrency}
