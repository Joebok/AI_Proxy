from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any

import pytest

from file_proxy.registry import Registry


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live-backends", action="store_true", help="run the opt-in real backend lifecycle scenario")
    parser.addoption("--live-runtime-config", default="runtime-config.local.json", help="machine-local config for the opt-in backend check")
    parser.addoption("--ollama-test-model", default=None, help="installed Ollama model to preload during the opt-in backend check")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live-backends"):
        return
    selected = [item for item in items if "live_backends" not in item.keywords]
    deselected = [item for item in items if "live_backends" in item.keywords]
    if deselected:
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for(predicate, *, timeout: float = 5.0, description: str = "condition") -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {description}")


def write_worker(directory: Path, *, counter: Path, gate: Path | None = None) -> Path:
    script = directory / "worker.py"
    script.write_text(
        "import argparse, json, os, pathlib, sys, time\n"
        "p=argparse.ArgumentParser(); p.add_argument('--job-dir', required=True); "
        "p.add_argument('--mode', default='success'); p.add_argument('--counter', required=True); "
        "p.add_argument('--gate'); a=p.parse_args(); d=pathlib.Path(a.job_dir)\n"
        "with open(a.counter, 'a', encoding='utf-8') as f: f.write(d.name + '\\n')\n"
        "if a.gate:\n"
        " gate=pathlib.Path(a.gate); deadline=time.monotonic()+8\n"
        " while not gate.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        " if not gate.exists(): raise SystemExit('gate timed out')\n"
        "if a.mode == 'failure': print('controlled worker failure', file=sys.stderr); raise SystemExit(7)\n"
        "if a.mode == 'unsafe': (d/'result.json').write_text(json.dumps({'artifact_path':'../escape'}), encoding='utf-8'); raise SystemExit(0)\n"
        "(d/'output.txt').write_text('processed:' + d.name, encoding='utf-8')\n",
        encoding="utf-8",
    )
    return script


def write_registry(directory: Path, worker: Path, *, counter: Path, gate: Path | None = None) -> Registry:
    directory.mkdir(parents=True, exist_ok=True)
    registration = {
        "command": [sys.executable, str(worker)],
        "timeout_seconds": 10,
        "backend": "ollama",
    }
    # Worker-specific flags belong in the trusted command, before the proxy appends --job-dir.
    registration["command"].extend(["--counter", str(counter)])
    if gate:
        registration["command"].extend(["--gate", str(gate)])
    (directory / "demo.json").write_text(json.dumps({"subscriber_id": "demo", "workers": {
        "success": {**registration, "command": [*registration["command"], "--mode", "success"]},
        "failure": {**registration, "command": [*registration["command"], "--mode", "failure"]},
        "unsafe": {**registration, "command": [*registration["command"], "--mode", "unsafe"]},
    }}), encoding="utf-8")
    return Registry.load(directory)


def publish_job(root: Path, job_id: str, *, worker: str = "success", created_at: str = "2026-01-01T00:00:00Z", inventory: list[dict[str, Any]] | None = None) -> Path:
    subscriber = root / "Ask" / "demo"
    subscriber.mkdir(parents=True, exist_ok=True)
    staging = subscriber / f".{job_id}.staging"
    staging.mkdir()
    payload = {"protocol_version": 1, "job_id": job_id, "subscriber_id": "demo", "worker": worker, "created_at": created_at}
    if inventory is not None:
        payload["files"] = inventory
    (staging / "job.json").write_text(json.dumps(payload), encoding="utf-8")
    ready = subscriber / job_id
    staging.replace(ready)
    return ready


def seed_running(root: Path, job_id: str, *, worker: str = "success") -> Path:
    directory = root / "Running" / "demo" / job_id
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({
        "protocol_version": 1, "job_id": job_id, "subscriber_id": "demo", "worker": worker,
        "created_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    return directory


def write_inventory(path: Path, content: bytes) -> dict[str, Any]:
    path.write_bytes(content)
    return {"path": path.name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def assert_process_dead(pid: int) -> None:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return
        try:
            exit_code = ctypes.c_ulong()
            assert ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            assert exit_code.value != 259, f"process {pid} is still active"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"process {pid} is still active")

