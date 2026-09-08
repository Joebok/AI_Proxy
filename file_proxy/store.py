from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator


class ExecutionStore:
    """Durable lifecycle metadata. Request bodies are deliberately never stored."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.cache_dir = runtime_dir / "cache"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = runtime_dir / "executions.sqlite3"
        self._lock = threading.RLock()
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    request_id TEXT PRIMARY KEY, backend TEXT NOT NULL,
                    prompt_id TEXT UNIQUE, state TEXT NOT NULL, outcome TEXT,
                    history_json BLOB, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    prompt_id TEXT NOT NULL, filename TEXT NOT NULL,
                    subfolder TEXT NOT NULL, type TEXT NOT NULL, cache_path TEXT NOT NULL,
                    size INTEGER NOT NULL, PRIMARY KEY(prompt_id, filename, subfolder, type)
                );
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create(self, request_id: str, backend: str, prompt_id: str | None) -> None:
        now = self._now()
        try:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO executions VALUES (?, ?, ?, 'accepted', NULL, NULL, NULL, ?, ?)",
                    (request_id, backend, prompt_id, now, now),
                )
        except sqlite3.IntegrityError as exc:
            if prompt_id:
                raise FileExistsError(prompt_id) from exc
            raise

    def update(self, request_id: str, state: str, *, outcome: str | None = None, history: bytes | None = None, error: str | None = None) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE executions SET state=?, outcome=COALESCE(?, outcome), history_json=COALESCE(?, history_json), error=COALESCE(?, error), updated_at=? WHERE request_id=?",
                (state, outcome, history, error, self._now(), request_id),
            )

    def reconcile_prompt_id(self, request_id: str, prompt_id: str) -> None:
        try:
            with self._connection() as db:
                db.execute("UPDATE executions SET prompt_id=?, updated_at=? WHERE request_id=?", (prompt_id, self._now(), request_id))
        except sqlite3.IntegrityError as exc:
            raise FileExistsError(prompt_id) from exc

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM executions WHERE request_id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def pending(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM executions WHERE state NOT IN ('completed','failed','cancelled') ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def history(self, prompt_id: str) -> bytes | None:
        with self._connection() as db:
            row = db.execute("SELECT history_json FROM executions WHERE prompt_id=? AND state IN ('completed','failed')", (prompt_id,)).fetchone()
        return bytes(row[0]) if row and row[0] is not None else None

    def histories(self) -> bytes:
        merged: dict[str, Any] = {}
        with self._connection() as db:
            rows = db.execute("SELECT history_json FROM executions WHERE state='completed' AND history_json IS NOT NULL ORDER BY updated_at").fetchall()
        for row in rows:
            try:
                value = json.loads(bytes(row[0]))
                if isinstance(value, dict):
                    merged.update(value)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return json.dumps(merged, separators=(",", ":")).encode()

    def delete_history(self, prompt_ids: list[str] | None = None) -> None:
        with self._connection() as db:
            if prompt_ids is None:
                rows = db.execute("SELECT request_id,prompt_id FROM executions WHERE state='completed'").fetchall()
            else:
                placeholders = ",".join("?" for _ in prompt_ids)
                rows = db.execute(f"SELECT request_id,prompt_id FROM executions WHERE prompt_id IN ({placeholders})", prompt_ids).fetchall() if prompt_ids else []
            for row in rows:
                for artifact in db.execute("SELECT cache_path FROM artifacts WHERE prompt_id=?", (row['prompt_id'],)).fetchall():
                    Path(artifact['cache_path']).unlink(missing_ok=True)
                db.execute("DELETE FROM artifacts WHERE prompt_id=?", (row['prompt_id'],))
                db.execute("DELETE FROM executions WHERE request_id=?", (row['request_id'],))

    def add_artifact(self, prompt_id: str, filename: str, subfolder: str, kind: str, path: Path, size: int) -> None:
        with self._connection() as db:
            db.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?, ?, ?)", (prompt_id, filename, subfolder, kind, str(path), size))

    def artifact(self, prompt_id: str | None, filename: str, subfolder: str, kind: str) -> Path | None:
        with self._connection() as db:
            if prompt_id:
                row = db.execute("SELECT cache_path FROM artifacts WHERE prompt_id=? AND filename=? AND subfolder=? AND type=?", (prompt_id, filename, subfolder, kind)).fetchone()
            else:
                row = db.execute("SELECT cache_path FROM artifacts WHERE filename=? AND subfolder=? AND type=? ORDER BY rowid DESC LIMIT 1", (filename, subfolder, kind)).fetchone()
        path = Path(row[0]) if row else None
        return path if path and path.is_file() else None

    def status(self) -> dict[str, Any]:
        with self._connection() as db:
            active = db.execute("SELECT COUNT(*) FROM executions WHERE state NOT IN ('completed','failed','cancelled')").fetchone()[0]
            cache_bytes = db.execute("SELECT COALESCE(SUM(size), 0) FROM artifacts").fetchone()[0]
        return {"active_records": active, "cache_bytes": cache_bytes, "runtime_dir": str(self.runtime_dir)}

    def cache_bytes(self) -> int:
        return int(self.status()["cache_bytes"])

    def evict(self, retention_days: int, max_bytes: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connection() as db:
            rows = db.execute("SELECT request_id, prompt_id FROM executions WHERE state IN ('completed','failed','cancelled') ORDER BY updated_at").fetchall()
            total = db.execute("SELECT COALESCE(SUM(size),0) FROM artifacts").fetchone()[0]
            for row in rows:
                stale = db.execute("SELECT updated_at < ? FROM executions WHERE request_id=?", (cutoff, row['request_id'])).fetchone()[0]
                if not stale and total <= max_bytes:
                    continue
                artifacts = db.execute("SELECT cache_path,size FROM artifacts WHERE prompt_id=?", (row['prompt_id'],)).fetchall()
                for artifact in artifacts:
                    Path(artifact['cache_path']).unlink(missing_ok=True)
                    total -= artifact['size']
                db.execute("DELETE FROM artifacts WHERE prompt_id=?", (row['prompt_id'],))
                db.execute("DELETE FROM executions WHERE request_id=?", (row['request_id'],))
