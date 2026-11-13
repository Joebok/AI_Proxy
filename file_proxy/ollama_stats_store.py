"""Local, prompt-free Ollama performance statistics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any


FIELDS = (
    "dispatch_time", "source", "job_key", "route", "requested_model", "effective_model",
    "outcome", "http_status", "queue_wait_ms", "first_byte_ms", "relay_ms",
    "total_duration_ns", "load_duration_ns", "prompt_eval_duration_ns", "eval_duration_ns",
    "prompt_eval_count", "prompt_eval_cached_count", "eval_count", "done_reason", "tokens_per_second",
    "num_ctx", "num_predict", "think", "reasoning_effort", "model_digest", "quantization",
    "context_length", "size_bytes", "size_vram_bytes", "gpu_percent", "cpu_percent", "residency_status",
)


class OllamaStatsStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.path = runtime_dir / "ollama_stats.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS ollama_stats (job_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, " + ", ".join(f"{field} TEXT" for field in FIELDS if field != "job_key") + ")")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=0.25)

    def write(self, record: dict[str, Any]) -> None:
        values = {field: record.get(field) for field in FIELDS}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        data_fields = tuple(field for field in FIELDS if field != "job_key")
        columns = ("job_key", "schema_version", *data_fields)
        with self._connect() as db:
            db.execute(f"INSERT OR REPLACE INTO ollama_stats ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                       (record["job_key"], 1, *(values[k] for k in data_fields)))
            db.execute("DELETE FROM ollama_stats WHERE dispatch_time < ?", (cutoff,))

    def rows(self):
        with self._connect() as db:
            db.execute("PRAGMA query_only=ON")
            return db.execute("SELECT " + ",".join(FIELDS) + " FROM ollama_stats ORDER BY dispatch_time, job_key").fetchall()
