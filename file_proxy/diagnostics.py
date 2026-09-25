from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any


class RequestDiagnosticLog:
    """Short-lived, body-free JSONL diagnostics for proxied HTTP requests."""

    def __init__(
        self,
        runtime_dir: Path,
        retention_days: int = 3,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("diagnostic log retention days must be positive")
        self.directory = runtime_dir / "request-diagnostics"
        self.retention_days = retention_days
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._last_cleanup: date | None = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cleanup(self._utc_now().date())

    def write(self, event: str, **details: Any) -> None:
        timestamp = self._utc_now()
        record = {
            "timestamp": timestamp.isoformat(),
            "event": event,
            **{key: value for key, value in details.items() if value is not None},
        }
        encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        with self._lock:
            day = timestamp.date()
            if self._last_cleanup != day:
                self._cleanup(day)
            path = self.directory / f"requests-{day.isoformat()}.jsonl"
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _cleanup(self, today: date) -> None:
        oldest = today - timedelta(days=self.retention_days - 1)
        for path in self.directory.glob("requests-*.jsonl"):
            try:
                log_date = date.fromisoformat(path.stem.removeprefix("requests-"))
            except ValueError:
                continue
            if log_date < oldest:
                path.unlink(missing_ok=True)
        self._last_cleanup = today
