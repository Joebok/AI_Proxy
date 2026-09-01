from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest

from file_proxy.engine import Proxy
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
