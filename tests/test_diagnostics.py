from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from file_proxy.diagnostics import RequestDiagnosticLog


def test_request_diagnostics_roll_daily_and_remove_expired_files(tmp_path: Path) -> None:
    directory = tmp_path / "request-diagnostics"
    directory.mkdir()
    (directory / "requests-2026-09-16.jsonl").write_text("expired\n", encoding="utf-8")
    (directory / "requests-2026-09-17.jsonl").write_text("retained\n", encoding="utf-8")
    (directory / "notes.jsonl").write_text("unrelated\n", encoding="utf-8")

    log = RequestDiagnosticLog(
        tmp_path,
        retention_days=3,
        now=lambda: datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
    )
    log.write(
        "terminal",
        request_id="request-1",
        elapsed_ms=1234,
        bytes=42,
        termination="upstream_timeout",
    )

    assert not (directory / "requests-2026-09-16.jsonl").exists()
    assert (directory / "requests-2026-09-17.jsonl").exists()
    assert (directory / "notes.jsonl").exists()
    record = json.loads((directory / "requests-2026-09-19.jsonl").read_text(encoding="utf-8"))
    assert record == {
        "timestamp": "2026-09-19T12:00:00+00:00",
        "event": "terminal",
        "request_id": "request-1",
        "elapsed_ms": 1234,
        "bytes": 42,
        "termination": "upstream_timeout",
    }
