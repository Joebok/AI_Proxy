from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import time
import uuid


@dataclass
class HttpJob:
    request_id: str
    started: asyncio.Future[None]
    finished: asyncio.Future[None]
    resource_key: str | None = None
    backend: str | None = None
    barrier: bool = False
    enqueued_at: float = 0.0
    cancelled: bool = False
    start_error: str | None = None
    dispatched: bool = False


class HttpQueue:
    """Shared HTTP admission queue with bounded affinity and control barriers."""

    def __init__(self, *, max_resource_streak: int = 5, oldest_override_seconds: float = 60.0) -> None:
        if max_resource_streak <= 0 or oldest_override_seconds < 0:
            raise ValueError("invalid HTTP scheduling limits")
        self._jobs: deque[HttpJob] = deque()
        self._changed = asyncio.Event()
        self._closed = False
        self.max_resource_streak = max_resource_streak
        self.oldest_override_seconds = oldest_override_seconds
        self._resource_key: str | None = None
        self._resource_streak = 0

    def enqueue(self, resource_key: str | None = None, *, backend: str | None = None, barrier: bool = False) -> HttpJob:
        if self._closed:
            raise RuntimeError("HTTP queue is shutting down")
        loop = asyncio.get_running_loop()
        job = HttpJob(
            request_id=str(uuid.uuid4()),
            started=loop.create_future(),
            finished=loop.create_future(),
            resource_key=resource_key,
            backend=backend,
            barrier=barrier,
            enqueued_at=loop.time(),
        )
        self._jobs.append(job)
        self._changed.set()
        return job

    @property
    def closed(self) -> bool:
        return self._closed

    def pop(self) -> HttpJob | None:
        self._jobs = deque(job for job in self._jobs if not job.cancelled)
        if not self._jobs:
            self._changed.clear()
            return None
        now = asyncio.get_running_loop().time()
        prefix: list[HttpJob] = []
        for job in self._jobs:
            prefix.append(job)
            if job.barrier:
                break
        first = prefix[0]
        if first.barrier or now - first.enqueued_at >= self.oldest_override_seconds:
            selected = first
        elif self._resource_key and self._resource_streak < self.max_resource_streak:
            selected = next((j for j in prefix if not j.barrier and j.resource_key == self._resource_key), first)
        else:
            selected = next((j for j in prefix if j.barrier or j.resource_key != self._resource_key), first)
        self._jobs.remove(selected)
        if selected.resource_key and selected.resource_key == self._resource_key:
            self._resource_streak += 1
        elif selected.resource_key:
            self._resource_key = selected.resource_key
            self._resource_streak = 1
        else:
            self._resource_key = None
            self._resource_streak = 0
        if not self._jobs:
            self._changed.clear()
        return selected

    def has_waiting(self) -> bool:
        return any(not job.cancelled for job in self._jobs)

    def snapshot(self) -> dict[str, object]:
        waiting = [job for job in self._jobs if not job.cancelled]
        now = time.monotonic()
        return {
            "queued": len(waiting),
            "oldest_wait_seconds": max((now - job.enqueued_at for job in waiting), default=0.0),
            "resource_affinity": self._resource_key,
            "resource_streak": self._resource_streak,
        }

    def cancel(self, job: HttpJob) -> None:
        job.cancelled = True
        if not job.finished.done():
            job.finished.set_result(None)
        self._changed.set()

    def finish(self, job: HttpJob) -> None:
        if not job.finished.done():
            job.finished.set_result(None)
        self._changed.set()

    async def wait(self, timeout: float) -> None:
        if self.has_waiting():
            return
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def close(self) -> None:
        self._closed = True
        for job in self._jobs:
            job.cancelled = True
            if not job.started.done():
                job.started.set_result(None)
            if not job.finished.done():
                job.finished.set_result(None)
        self._jobs.clear()
        self._changed.set()
