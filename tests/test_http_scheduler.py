from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from file_proxy.engine import Proxy
from file_proxy.models import JobManifest, QueueJob
from file_proxy.registry import Registry
from file_proxy.scheduler import HttpQueue


def make_proxy(tmp_path: Path) -> Proxy:
    proxy = Proxy(tmp_path / "File_Proxy", Registry({}))
    proxy.ensure_layout()
    return proxy


def test_http_queue_is_fifo(tmp_path: Path) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        queue = HttpQueue()
        first = queue.enqueue()
        second = queue.enqueue()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            await asyncio.wait_for(first.started, 1)
            assert not second.started.done()
            queue.finish(first)
            await asyncio.wait_for(second.started, 1)
            queue.finish(second)
        finally:
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())


def test_managed_scheduler_publishes_invalid_jobs_without_starting_backend(tmp_path: Path) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        invalid = (tmp_path / "invalid-job", "test", "invalid-job", "invalid input")
        selections = [invalid]
        proxy._select_file_job = lambda: selections.pop(0) if selections else None
        processed = threading.Event()
        proxy._process_file_job = lambda selected: processed.set() if selected == invalid else None

        class ManagedBackend:
            config = SimpleNamespace(enabled=True)

            async def prepare_for(self, *_args, **_kwargs):
                pytest.fail("Invalid jobs must not start a backend")

            async def released(self, *_args):
                pytest.fail("Invalid jobs did not acquire a backend")

        scheduler = asyncio.create_task(proxy.run_scheduler(
            HttpQueue(), poll_seconds=0.01, continuation_grace_seconds=0,
            backend_manager=ManagedBackend(),
        ))
        try:
            assert await asyncio.to_thread(processed.wait, 1)
        finally:
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())


def test_http_affinity_does_not_overtake_other_backend() -> None:
    async def scenario() -> None:
        queue = HttpQueue()
        first = queue.enqueue("ollama:a", backend="ollama")
        comfy = queue.enqueue("comfyui:model", backend="comfyui")
        matching = queue.enqueue("ollama:a", backend="ollama")

        assert queue.pop() is first
        queue.finish(first)
        assert queue.pop() is comfy
        queue.finish(comfy)
        assert queue.pop() is matching

        reverse = HttpQueue()
        first_comfy = reverse.enqueue("comfyui:model", backend="comfyui")
        ollama = reverse.enqueue("ollama:a", backend="ollama")
        matching_comfy = reverse.enqueue("comfyui:model", backend="comfyui")

        assert reverse.pop() is first_comfy
        reverse.finish(first_comfy)
        assert reverse.pop() is ollama
        reverse.finish(ollama)
        assert reverse.pop() is matching_comfy

    asyncio.run(scenario())


def test_completed_http_resource_drives_next_file_selection(tmp_path: Path) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        queue = HttpQueue()
        file_started = threading.Event()
        selections = [object()]
        proxy._select_file_job = lambda: selections.pop(0) if selections else None  # type: ignore[method-assign]

        def process_file(_selected) -> None:
            assert proxy._resource_key == "ollama:vision"
            assert proxy._resource_streak == 1
            file_started.set()

        proxy._process_file_job = process_file  # type: ignore[method-assign]
        http_job = queue.enqueue("ollama:vision")
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            await asyncio.wait_for(http_job.started, 1)
            queue.finish(http_job)
            assert await asyncio.to_thread(file_started.wait, 1)
        finally:
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())


def test_http_has_preference_and_continuation_grace(tmp_path: Path) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        queue = HttpQueue()
        file_job = object()
        selections = [file_job]
        file_started = threading.Event()
        proxy._select_file_job = lambda: selections.pop(0) if selections else None  # type: ignore[method-assign]
        proxy._process_file_job = lambda _selected: file_started.set()  # type: ignore[method-assign]

        first = queue.enqueue()
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0.1)
        )
        try:
            await asyncio.wait_for(first.started, 1)
            assert not file_started.is_set()
            queue.finish(first)
            await asyncio.sleep(0.02)
            second = queue.enqueue()
            await asyncio.wait_for(second.started, 1)
            assert not file_started.is_set()
            queue.finish(second)
            assert await asyncio.to_thread(file_started.wait, 1)
        finally:
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("file_backend", "http_backend"),
    [("comfyui", "ollama"), ("ollama", "comfyui")],
)
def test_http_does_not_overtake_older_file_job_from_other_backend(
    tmp_path: Path, file_backend: str, http_backend: str
) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        queue = HttpQueue()
        file_started = threading.Event()
        release_file = threading.Event()
        selected = QueueJob(
            tmp_path / "file-job",
            JobManifest(
                protocol_version=1,
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                resource_key=f"{file_backend}:model",
                subscriber_id="test",
                worker="worker",
                job_id="file-job",
            ),
        )
        selections = [selected]
        proxy._select_file_job = lambda: selections.pop(0) if selections else None  # type: ignore[method-assign]
        proxy._file_job_backend = lambda _job: file_backend  # type: ignore[method-assign]
        def process_file(_job) -> None:
            file_started.set()
            release_file.wait(1)

        proxy._process_file_job = process_file  # type: ignore[method-assign]
        http_job = queue.enqueue(f"{http_backend}:model", backend=http_backend)
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            assert await asyncio.to_thread(file_started.wait, 1)
            assert not http_job.started.done()
            release_file.set()
            await asyncio.wait_for(http_job.started, 1)
            queue.finish(http_job)
        finally:
            release_file.set()
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())


def test_running_file_job_is_not_preempted(tmp_path: Path) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        queue = HttpQueue()
        file_job = object()
        selections = [file_job]
        file_started = threading.Event()
        release_file = threading.Event()

        proxy._select_file_job = lambda: selections.pop(0) if selections else None  # type: ignore[method-assign]

        def process_file(_selected) -> None:
            file_started.set()
            release_file.wait(1)

        proxy._process_file_job = process_file  # type: ignore[method-assign]
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            assert await asyncio.to_thread(file_started.wait, 1)
            http_job = queue.enqueue()
            await asyncio.sleep(0.05)
            assert not http_job.started.done()
            release_file.set()
            await asyncio.wait_for(http_job.started, 1)
            queue.finish(http_job)
        finally:
            release_file.set()
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())


def test_cancelled_http_job_is_skipped(tmp_path: Path) -> None:
    async def scenario() -> None:
        proxy = make_proxy(tmp_path)
        queue = HttpQueue()
        first = queue.enqueue()
        cancelled = queue.enqueue()
        following = queue.enqueue()
        queue.cancel(cancelled)
        scheduler = asyncio.create_task(
            proxy.run_scheduler(queue, poll_seconds=0.01, continuation_grace_seconds=0)
        )
        try:
            await asyncio.wait_for(first.started, 1)
            queue.finish(first)
            await asyncio.wait_for(following.started, 1)
            assert not cancelled.started.done()
            queue.finish(following)
        finally:
            scheduler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scheduler

    asyncio.run(scenario())
