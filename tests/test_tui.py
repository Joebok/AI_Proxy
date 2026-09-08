from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import socket
import sys

from textual.widgets import RichLog, Static

from file_proxy.cli import parser
from file_proxy.engine import Proxy
from file_proxy.http_proxy import HttpProxyConfig, OLLAMA_PROFILE
from file_proxy.registry import Registry
from file_proxy.runtime import RuntimeConfig
from file_proxy.supervisor import (
    ListenerSnapshot,
    LogEntry,
    RuntimeSnapshot,
    RuntimeSupervisor,
)
from file_proxy.tui import ProxyDashboard


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_supervisor(tmp_path: Path, configs: list[HttpProxyConfig] | None = None) -> RuntimeSupervisor:
    runtime_dir = tmp_path / "runtime"
    proxy = Proxy(
        tmp_path / "queue",
        Registry({}),
        local_runtime_dir=runtime_dir,
    )
    return RuntimeSupervisor(
        proxy,
        configs or [],
        RuntimeConfig(runtime_dir=str(runtime_dir)),
        poll_seconds=0.01,
    )


def test_filesystem_runtime_stops_restarts_and_releases_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor = make_supervisor(tmp_path)
        lock = tmp_path / "runtime" / "proxy.lock"
        assert await supervisor.start()
        assert lock.is_file()
        assert (await supervisor.snapshot()).state == "ready"
        assert await supervisor.stop()
        assert supervisor.state == "stopped" and not lock.exists()
        assert await supervisor.restart()
        assert lock.is_file()
        await supervisor.stop()
        assert not lock.exists()

    asyncio.run(scenario())


def test_http_runtime_reports_listener_and_rebinds_on_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        port = unused_port()
        config = HttpProxyConfig(
            listen_port=port,
            upstream="http://127.0.0.1:65530",
            profile=OLLAMA_PROFILE,
        )
        supervisor = make_supervisor(tmp_path, [config])
        assert await supervisor.start()
        snapshot = await supervisor.snapshot()
        assert snapshot.listeners[0].state == "listening"
        assert snapshot.listeners[0].port == port
        assert snapshot.http_queued == 0
        assert await supervisor.restart()
        assert (await supervisor.snapshot()).listeners[0].state == "listening"
        await supervisor.stop()
        assert (await supervisor.snapshot()).listeners[0].state == "stopped"

    asyncio.run(scenario())


def test_stop_waits_for_active_filesystem_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = tmp_path / "worker.py"
        worker.write_text(
            "import argparse, pathlib, time\n"
            "p=argparse.ArgumentParser(); p.add_argument('--job-dir'); a=p.parse_args()\n"
            "time.sleep(0.25); pathlib.Path(a.job_dir, 'done.txt').write_text('done')\n",
            encoding="utf-8",
        )
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        (registry_dir / "test.json").write_text(
            json.dumps(
                {
                    "subscriber_id": "test",
                    "workers": {
                        "slow": {
                            "command": [sys.executable, str(worker)],
                            "timeout_seconds": 5,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        registry = Registry.load(registry_dir)
        runtime_dir = tmp_path / "runtime"
        proxy = Proxy(tmp_path / "queue", registry, local_runtime_dir=runtime_dir)
        job = proxy.ask_root / "test" / "job-1"
        job.mkdir(parents=True)
        (job / "job.json").write_text(
            json.dumps(
                {
                    "protocol_version": 1,
                    "job_id": "job-1",
                    "subscriber_id": "test",
                    "worker": "slow",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        supervisor = RuntimeSupervisor(
            proxy,
            [],
            RuntimeConfig(runtime_dir=str(runtime_dir)),
            poll_seconds=0.01,
        )
        await supervisor.start()
        running = proxy.running_root / "test" / "job-1"
        for _ in range(100):
            if running.exists():
                break
            await asyncio.sleep(0.01)
        assert running.exists()
        stop = asyncio.create_task(supervisor.stop())
        await asyncio.sleep(0.02)
        assert not stop.done()
        assert await stop
        assert (proxy.answer_root / "test" / "job-1" / "done.txt").is_file()
        assert not (runtime_dir / "proxy.lock").exists()

    asyncio.run(scenario())


def test_listener_bind_failure_is_visible_without_exiting_dashboard(tmp_path: Path) -> None:
    async def scenario() -> None:
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = int(occupied.getsockname()[1])
        try:
            supervisor = make_supervisor(
                tmp_path,
                [HttpProxyConfig(listen_port=port, profile=OLLAMA_PROFILE)],
            )
            assert not await supervisor.start()
            snapshot = await supervisor.snapshot()
            assert snapshot.state == "error"
            assert snapshot.error
            assert snapshot.listeners[0].state == "error"
            assert not (tmp_path / "runtime" / "proxy.lock").exists()
        finally:
            occupied.close()

    asyncio.run(scenario())


class FakeSupervisor:
    def __init__(self) -> None:
        self.state = "stopped"
        self.transitioning = False
        self.started = 0
        self.stopped = 0
        self.restarted = 0

    async def start(self) -> bool:
        self.started += 1
        self.state = "ready"
        return True

    async def stop(self) -> bool:
        self.stopped += 1
        self.state = "stopped"
        return True

    async def restart(self) -> bool:
        self.restarted += 1
        self.state = "ready"
        return True

    async def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state=self.state,
            error=None,
            listeners=(
                ListenerSnapshot("ollama", "127.0.0.1", 11433, "http://127.0.0.1:11434", "listening"),
                ListenerSnapshot("comfyui", "127.0.0.1", 18188, "http://127.0.0.1:8188", "listening"),
            ),
            filesystem_counts={
                "queued": 2,
                "running": 0,
                "succeeded": 4,
                "failed": 1,
                "invalid": 0,
            },
            http_queued=3,
            http_oldest_wait_seconds=1.25,
            admitted_requests=3,
            buffered_body_bytes=1024,
            active_backend=None,
            active_job=None,
            filesystem_degraded=None,
            backend_status={
                "state": "stopped",
                "ownership": "proxy",
                "blocked_reason": None,
            },
            cache_status=None,
            logs=(LogEntry(1, datetime.now().astimezone(), "test", "success", "Ready for work"),),
        )


def test_dashboard_renders_status_log_and_shortcuts() -> None:
    async def scenario() -> None:
        supervisor = FakeSupervisor()
        app = ProxyDashboard(supervisor)  # type: ignore[arg-type]
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            state = app.query_one("#state", Static)
            listeners = app.query_one("#listeners", Static)
            queues = app.query_one("#queues", Static)
            assert state.has_class("state-ready")
            listener_text = str(listeners.render())
            assert "ComfyUI proxy" in listener_text
            assert "on demand" in listener_text
            assert listeners.content_region.height >= 7
            assert "Filesystem" in str(queues.render())
            assert len(app.query_one("#log", RichLog).lines) == 1
            await pilot.press("s")
            await pilot.pause()
            assert supervisor.stopped == 1
            await pilot.press("r")
            await pilot.pause()
            assert supervisor.restarted == 1
            await pilot.press("pageup")
            await pilot.press("pagedown")
            await pilot.press("end")

    asyncio.run(scenario())


def test_tui_cli_matches_run_options_and_launchers_select_it() -> None:
    args = parser().parse_args(
        [
            "--root",
            "queue",
            "--registry-dir",
            "registries",
            "tui",
            "--http-listen-port",
            "11433",
            "--comfyui-listen-port",
            "18188",
            "--poll-seconds",
            "0.5",
        ]
    )
    assert args.command == "tui"
    assert (args.http_listen_port, args.comfyui_listen_port, args.poll_seconds) == (
        11433,
        18188,
        0.5,
    )
    root = Path(__file__).parents[1]
    assert '@("tui", "--sync-grace-seconds"' in (root / "run_file_proxy.ps1").read_text()
    assert " tui --sync-grace-seconds " in (root / "run_file_proxy.bat").read_text()
