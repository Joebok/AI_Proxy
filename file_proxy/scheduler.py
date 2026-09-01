from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import uuid


@dataclass
class HttpJob:
    request_id: str
    started: asyncio.Future[None]
    finished: asyncio.Future[None]
    resource_key: str | None = None
    cancelled: bool = False


class HttpQueue:
    def __init__(self) -> None:
        self._jobs: deque[HttpJob] = deque()
        self._changed = asyncio.Event()
        self._closed = False

    def enqueue(self, resource_key: str | None = None) -> HttpJob:
        if self._closed:
            raise RuntimeError("HTTP queue is shutting down")
        loop = asyncio.get_running_loop()
        job = HttpJob(
            request_id=uuid.uuid4().hex,
            started=loop.create_future(),
            finished=loop.create_future(),
            resource_key=resource_key,
        )
        self._jobs.append(job)
        self._changed.set()
        return job

    @property
    def closed(self) -> bool:
        return self._closed

    def pop(self) -> HttpJob | None:
        while self._jobs:
            job = self._jobs.popleft()
            if not job.cancelled:
                if not self._jobs:
                    self._changed.clear()
                return job
        self._changed.clear()
        return None

    def has_waiting(self) -> bool:
        return any(not job.cancelled for job in self._jobs)

    def cancel(self, job: HttpJob) -> None:
        job.cancelled = True
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
        self._jobs.clear()
        self._changed.set()
