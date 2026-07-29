from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Any
from collections.abc import Callable

from .models import InvalidJob, JobManifest, JobNotReady, ProxyResult, QueueJob, iso_utc, utc_now
from .registry import Registry
from .validation import build_file_inventory, validate_job_folder


class ProxyAlreadyRunning(RuntimeError):
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
            json.dump(
                {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "started_at": iso_utc(utc_now()),
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
                import ctypes

                process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if not process:
                    return False
                ctypes.windll.kernel32.CloseHandle(process)
                return True
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
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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
    ):
        requested_root = root.resolve()
        self.root = (
            requested_root
            if requested_root.name.casefold() == "file_proxy"
            else requested_root / "File_Proxy"
        )
        self.registry = registry
        self.logger = logger or (lambda _message: None)
        self.sync_grace_seconds = sync_grace_seconds
        self._not_ready_since: dict[Path, float] = {}
        self.ask_root = self.root / "Ask"
        self.running_root = self.root / "Running"
        self.answer_root = self.root / "Answer"
        self.control_root = self.root / "Control"

    def ensure_layout(self) -> None:
        for path in (self.ask_root, self.running_root, self.answer_root, self.control_root):
            path.mkdir(parents=True, exist_ok=True)

    def lock(self) -> RuntimeLock:
        return RuntimeLock(self.control_root / "proxy.lock")

    def _answer_path(self, subscriber_id: str, job_id: str) -> Path:
        return self.answer_root / subscriber_id / job_id

    def _move_to_answer(self, path: Path, subscriber_id: str, job_id: str) -> Path:
        destination = self._answer_path(subscriber_id, job_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError(f"answer already exists: {destination}")
        _replace_with_retry(path, destination)
        return destination

    def _invalid(self, path: Path, subscriber_id: str, job_id: str, message: str) -> Path:
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
            output_files=_inventory_with_retry(path),
        )
        if path.is_symlink():
            path.unlink()
            path.mkdir()
        _write_json_atomic(path / "proxy_result.json", result.to_dict())
        return self._move_to_answer(path, subscriber_id, job_id)

    def recover_interrupted(self) -> int:
        recovered = 0
        for subscriber_dir in sorted(path for path in self.running_root.iterdir() if path.is_dir()):
            for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir()):
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
                _write_json_atomic(job_dir / "proxy_result.json", result.to_dict())
                self._move_to_answer(job_dir, subscriber_dir.name, job_dir.name)
                self.logger(f"Recovered interrupted job {subscriber_dir.name}/{job_dir.name} as FAILED")
                recovered += 1
        return recovered

    def _scan(self) -> tuple[list[QueueJob], list[tuple[Path, str, str, str]]]:
        jobs: list[QueueJob] = []
        invalid: list[tuple[Path, str, str, str]] = []
        for subscriber_dir in sorted(path for path in self.ask_root.iterdir() if path.is_dir()):
            if subscriber_dir.name.startswith("."):
                continue
            if subscriber_dir.is_symlink():
                continue
            for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir()):
                if job_dir.name.startswith("."):
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
        jobs.sort(key=lambda item: (item.manifest.created_at, item.manifest.subscriber_id, item.manifest.job_id))
        return jobs, invalid

    def process_once(self) -> Path | None:
        jobs, invalid = self._scan()
        if invalid:
            return self._invalid(*invalid[0])
        if not jobs:
            return None
        queued = jobs[0]
        manifest = queued.manifest
        self.logger(
            f"Starting {manifest.subscriber_id}/{manifest.job_id} "
            f"with worker {manifest.worker}"
        )
        registration = self.registry.worker(manifest.subscriber_id, manifest.worker)
        assert registration is not None
        running = self.running_root / manifest.subscriber_id / manifest.job_id
        running.parent.mkdir(parents=True, exist_ok=True)
        _replace_with_retry(queued.path, running)
        started = utc_now()
        status = "SUCCEEDED"
        exit_code: int | None = None
        error_type: str | None = None
        error_message: str | None = None
        stdout = ""
        stderr = ""
        try:
            completed = subprocess.run(
                [*registration.command, "--job-dir", str(running)],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=registration.timeout_seconds,
                check=False,
                cwd=registration.working_directory,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            if completed.returncode != 0:
                status = "FAILED"
                error_type = "WORKER_EXIT"
                error_message = f"Worker exited with code {completed.returncode}."
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
        if not running.is_dir():
            running.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(
                running / "job.json",
                {
                    "protocol_version": manifest.protocol_version,
                    "job_id": manifest.job_id,
                    "subscriber_id": manifest.subscriber_id,
                    "worker": manifest.worker,
                    "created_at": iso_utc(manifest.created_at),
                },
            )
            status = "FAILED"
            error_type = "INVALID_OUTPUT_STATE"
            error_message = "Worker removed its job folder."
        else:
            try:
                validate_job_folder(running, manifest.subscriber_id, manifest.job_id)
            except (InvalidJob, JobNotReady) as exc:
                status = "FAILED"
                error_type = "INVALID_OUTPUT_STATE"
                error_message = str(exc)
        completed_at = utc_now()
        output_files = _inventory_with_retry(running)
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
            output_files=output_files,
        )
        _write_json_atomic(running / "proxy_result.json", result.to_dict())
        answer = self._move_to_answer(running, manifest.subscriber_id, manifest.job_id)
        self.logger(
            f"Finished {manifest.subscriber_id}/{manifest.job_id}: "
            f"{status} in {result.duration_seconds:.3f}s"
        )
        return answer

    def run(self, poll_seconds: float = 1.0) -> None:
        self.ensure_layout()
        self.logger(f"Queue root: {self.root}")
        with self.lock():
            self.logger(f"Lock acquired: {self.control_root / 'proxy.lock'}")
            recovered = self.recover_interrupted()
            if recovered:
                self.logger(f"Recovered {recovered} interrupted job(s)")
            self.logger(f"Ready; polling every {poll_seconds:g}s. Press Ctrl-C to stop.")
            idle = False
            while True:
                if self.process_once() is None:
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
            return self.process_once()

    def status(self, subscriber: str | None = None, job: str | None = None) -> dict[str, Any]:
        self.ensure_layout()
        counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "invalid": 0}
        items: list[dict[str, str]] = []
        for state, root in (("queued", self.ask_root), ("running", self.running_root), ("answer", self.answer_root)):
            for subscriber_dir in sorted(path for path in root.iterdir() if path.is_dir()):
                if subscriber and subscriber_dir.name != subscriber:
                    continue
                for job_dir in sorted(path for path in subscriber_dir.iterdir() if path.is_dir() and not path.name.startswith(".")):
                    if job and job_dir.name != job:
                        continue
                    item_state = state
                    if state == "answer":
                        try:
                            result = json.loads((job_dir / "proxy_result.json").read_text(encoding="utf-8"))
                            item_state = str(result.get("status", "FAILED")).lower()
                        except (OSError, json.JSONDecodeError):
                            item_state = "failed"
                    counts[item_state] += 1
                    items.append({"subscriber_id": subscriber_dir.name, "job_id": job_dir.name, "status": item_state})
        return {"counts": counts, "jobs": items}
