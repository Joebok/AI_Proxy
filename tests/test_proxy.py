from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

import pytest

from file_proxy.engine import Proxy, ProxyAlreadyRunning, RuntimeLock, _replace_with_retry
from file_proxy.registry import Registry


def write_registry(path: Path) -> Registry:
    path.mkdir()
    worker = path.parent / "worker.py"
    worker.write_text(
        "import argparse, pathlib\n"
        "p=argparse.ArgumentParser(); p.add_argument('--job-dir', required=True); a=p.parse_args()\n"
        "d=pathlib.Path(a.job_dir); (d/'output.txt').write_text(d.name, encoding='utf-8')\n",
        encoding="utf-8",
    )
    failing = path.parent / "failing.py"
    failing.write_text(
        "import argparse, sys\n"
        "p=argparse.ArgumentParser(); p.add_argument('--job-dir'); p.parse_args()\n"
        "print('out'); print('err', file=sys.stderr); raise SystemExit(7)\n",
        encoding="utf-8",
    )
    slow = path.parent / "slow.py"
    slow.write_text(
        "import argparse, time\n"
        "p=argparse.ArgumentParser(); p.add_argument('--job-dir'); p.parse_args(); time.sleep(2)\n",
        encoding="utf-8",
    )
    unsafe = path.parent / "unsafe.py"
    unsafe.write_text(
        "import argparse, json, pathlib\n"
        "p=argparse.ArgumentParser(); p.add_argument('--job-dir'); a=p.parse_args()\n"
        "d=pathlib.Path(a.job_dir); (d/'output.json').write_text(json.dumps({'output_path':'../escape'}))\n",
        encoding="utf-8",
    )
    (path / "zet.json").write_text(
        json.dumps(
            {
                "subscriber_id": "zet",
                "workers": {
                    "mock": {"command": [sys.executable, str(worker)], "timeout_seconds": 5},
                    "missing": {"command": [str(path / "does-not-exist")], "timeout_seconds": 5},
                    "failing": {"command": [sys.executable, str(failing)], "timeout_seconds": 5},
                    "slow": {"command": [sys.executable, str(slow)], "timeout_seconds": 0.05},
                    "unsafe": {"command": [sys.executable, str(unsafe)], "timeout_seconds": 5},
                },
            }
        ),
        encoding="utf-8",
    )
    return Registry.load(path)


def publish(root: Path, job_id: str, *, created_at: str, worker: str = "mock") -> Path:
    parent = root / "Ask" / "zet"
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{job_id}.staging"
    staging.mkdir()
    (staging / "job.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "job_id": job_id,
                "subscriber_id": "zet",
                "worker": worker,
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )
    ready = parent / job_id
    staging.replace(ready)
    return ready


def make_proxy(tmp_path: Path) -> Proxy:
    return Proxy(tmp_path / "File_Proxy", write_registry(tmp_path / "registry"))


def test_registry_expands_machine_local_project_root(tmp_path: Path, monkeypatch) -> None:
    registry_dir = tmp_path / "registry"
    project = tmp_path / "Zet"
    registry_dir.mkdir()
    project.mkdir()
    monkeypatch.setenv("ZET_PROJECT_ROOT", str(project))
    (registry_dir / "zet.json").write_text(
        json.dumps(
            {
                "subscriber_id": "zet",
                "workers": {
                    "mock": {
                        "command": ["python3", "${ZET_PROJECT_ROOT}/worker.py"],
                        "working_directory": "${ZET_PROJECT_ROOT}",
                        "timeout_seconds": 5,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    worker = Registry.load(registry_dir).worker("zet", "mock")

    assert worker is not None
    assert worker.command[1] == f"{project}/worker.py"
    assert worker.working_directory == str(project)


def test_base_queue_root_is_normalized_to_file_proxy(tmp_path: Path) -> None:
    proxy = Proxy(tmp_path / "AI_Queue", write_registry(tmp_path / "registry"))
    assert proxy.root == (tmp_path / "AI_Queue" / "File_Proxy").resolve()


def test_transition_retries_sharing_violation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    real_replace = __import__("os").replace
    attempts = 0

    def flaky_replace(src, dest):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "sharing violation")
        real_replace(src, dest)

    with patch("file_proxy.engine.os.replace", side_effect=flaky_replace):
        _replace_with_retry(source, destination, timeout_seconds=1)

    assert attempts == 3
    assert destination.is_dir()


def test_success_and_fifo(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    publish(proxy.root, "later", created_at="2026-01-02T00:00:00Z")
    publish(proxy.root, "first", created_at="2026-01-01T00:00:00Z")
    answer = proxy.once()
    assert answer == proxy.answer_root / "zet" / "first"
    assert (answer / "output.txt").read_text(encoding="utf-8") == "first"
    assert json.loads((answer / "proxy_result.json").read_text())["status"] == "SUCCEEDED"
    assert (proxy.ask_root / "zet" / "later").exists()


def test_staging_is_ignored(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    proxy.ensure_layout()
    staging = proxy.ask_root / "zet" / ".partial.staging"
    staging.mkdir(parents=True)
    assert proxy.once() is None
    assert staging.exists()


def test_incomplete_synced_job_waits_until_declared_file_arrives(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    job = publish(proxy.root, "syncing", created_at="2026-01-01T00:00:00Z")
    manifest = json.loads((job / "job.json").read_text())
    manifest["files"] = [{"path": "prompt.md", "size": 5, "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}]
    (job / "job.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert proxy.once() is None
    assert job.exists()

    (job / "prompt.md").write_text("hello", encoding="utf-8")
    answer = proxy.once()
    assert json.loads((answer / "proxy_result.json").read_text())["status"] == "SUCCEEDED"


def test_incomplete_synced_job_expires_after_grace_period(tmp_path: Path) -> None:
    proxy = Proxy(
        tmp_path / "File_Proxy",
        write_registry(tmp_path / "registry"),
        sync_grace_seconds=0,
    )
    job = publish(proxy.root, "never-syncs", created_at="2026-01-01T00:00:00Z")
    manifest = json.loads((job / "job.json").read_text())
    manifest["files"] = [{"path": "missing.md", "size": 1, "sha256": "0" * 64}]
    (job / "job.json").write_text(json.dumps(manifest), encoding="utf-8")

    answer = proxy.once()
    result = json.loads((answer / "proxy_result.json").read_text())
    assert result["status"] == "INVALID"
    assert "sync did not complete" in result["error_message"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(protocol_version=2), "protocol_version"),
        (lambda data: data.update(job_id="../escape"), "job_id"),
        (lambda data: data.update(subscriber_id="other"), "subscriber_id"),
        (lambda data: data.update(created_at="yesterday"), "created_at"),
        (lambda data: data.update(worker="unknown"), "unknown worker"),
    ],
)
def test_invalid_jobs_publish_invalid_answer(tmp_path: Path, mutate, message: str) -> None:
    proxy = make_proxy(tmp_path)
    job = publish(proxy.root, "bad", created_at="2026-01-01T00:00:00Z")
    data = json.loads((job / "job.json").read_text())
    mutate(data)
    (job / "job.json").write_text(json.dumps(data), encoding="utf-8")
    answer = proxy.once()
    result = json.loads((answer / "proxy_result.json").read_text())
    assert result["status"] == "INVALID"
    assert message in result["error_message"]


def test_unsafe_payload_path_is_invalid(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    job = publish(proxy.root, "badpath", created_at="2026-01-01T00:00:00Z")
    (job / "payload.json").write_text(json.dumps({"prompt_file": "../secret"}), encoding="utf-8")
    answer = proxy.once()
    assert json.loads((answer / "proxy_result.json").read_text())["status"] == "INVALID"


def test_api_route_is_not_treated_as_filesystem_path(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    job = publish(proxy.root, "api-route", created_at="2026-01-01T00:00:00Z")
    (job / "Stable_Matrix_API_Call.json").write_text(
        json.dumps({"api_path": "/sdapi/v1/txt2img"}),
        encoding="utf-8",
    )
    answer = proxy.once()
    assert json.loads((answer / "proxy_result.json").read_text())["status"] == "SUCCEEDED"


def test_escaping_symlink_is_invalid(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    job = publish(proxy.root, "badlink", created_at="2026-01-01T00:00:00Z")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (job / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    result = json.loads((proxy.once() / "proxy_result.json").read_text())
    assert result["status"] == "INVALID"


def test_launch_failure_is_answered_without_retry(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    publish(proxy.root, "launch", created_at="2026-01-01T00:00:00Z", worker="missing")
    answer = proxy.once()
    result = json.loads((answer / "proxy_result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["error_type"] == "WORKER_LAUNCH"
    assert not (proxy.ask_root / "zet" / "launch").exists()


def test_nonzero_exit_captures_logs(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    publish(proxy.root, "failed", created_at="2026-01-01T00:00:00Z", worker="failing")
    result = json.loads((proxy.once() / "proxy_result.json").read_text())
    assert (result["status"], result["exit_code"]) == ("FAILED", 7)
    assert result["stdout"].strip() == "out"
    assert result["stderr"].strip() == "err"


def test_timeout_is_answered(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    publish(proxy.root, "timeout", created_at="2026-01-01T00:00:00Z", worker="slow")
    result = json.loads((proxy.once() / "proxy_result.json").read_text())
    assert result["error_type"] == "WORKER_TIMEOUT"


def test_unsafe_worker_output_fails_job(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    publish(proxy.root, "unsafe", created_at="2026-01-01T00:00:00Z", worker="unsafe")
    result = json.loads((proxy.once() / "proxy_result.json").read_text())
    assert result["error_type"] == "INVALID_OUTPUT_STATE"


def test_lock_refuses_concurrent_instance(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    proxy.ensure_layout()
    with proxy.lock():
        with pytest.raises(ProxyAlreadyRunning):
            with proxy.lock():
                pass


def test_foreign_host_lock_is_never_reclaimed_from_dropbox(tmp_path: Path) -> None:
    lock_path = tmp_path / "proxy.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "hostname": f"other-than-{socket.gethostname()}",
                "started_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProxyAlreadyRunning):
        with RuntimeLock(lock_path):
            pass
    assert lock_path.exists()


def test_interrupted_running_job_becomes_failed_answer(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    proxy.ensure_layout()
    job = publish(proxy.root, "interrupted", created_at="2026-01-01T00:00:00Z")
    running = proxy.running_root / "zet" / job.name
    running.parent.mkdir(parents=True)
    job.replace(running)
    assert proxy.recover_interrupted() == 1
    answer = proxy.answer_root / "zet" / "interrupted"
    result = json.loads((answer / "proxy_result.json").read_text())
    assert result["status"] == "FAILED"
    assert result["error_type"] == "PROXY_INTERRUPTED"


def test_status_filters_jobs(tmp_path: Path) -> None:
    proxy = make_proxy(tmp_path)
    publish(proxy.root, "one", created_at=datetime.now(timezone.utc).isoformat())
    proxy.once()
    status = proxy.status(subscriber="zet", job="one")
    assert status["counts"]["succeeded"] == 1
