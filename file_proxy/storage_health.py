"""Small local journal for removable storage; never create a monitored root."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import errno
import json
import os
import sqlite3
from pathlib import Path
import threading
import time
from uuid import uuid4


class StorageUnavailable(OSError):
    """A configured resource is temporarily unavailable, not empty."""


def stamp():
    return datetime.now(timezone.utc).isoformat()


class StorageHealth:
    def __init__(self, journal: Path, resources: dict[str, Path]):
        self.journal = Path(journal).resolve()
        self.resources = {key: Path(path).resolve() for key, path in resources.items()}
        for path in self.resources.values():
            if self.journal == path or path in self.journal.parents:
                raise ValueError("Recovery journal must be outside monitored storage")
            if os.name == "nt" and path.drive and path.drive.casefold() == self.journal.drive.casefold():
                # Fixed local resources may share C:, but recovery cannot depend on E:.
                system_drive = os.environ.get("SystemDrive", "C:")
                if path.drive.casefold() != system_drive.casefold():
                    raise ValueError("Recovery journal must be on the local system volume")
        self._lock = threading.RLock()
        self._last_probe = 0.0
        self._successes = {}
        self._last_success = {}
        self._probe_thread = None
        self._probe_event = None
        self._probing_name = None
        self._probe_timed_out = False
        self._rows = {}
        try:
            self._rows = json.loads(self.journal.read_text(encoding="utf-8")).get("resources", {})
        except FileNotFoundError:
            pass
        for name, row in self._rows.items():
            if name.startswith("catalog:") and Path(row["path"]).parent == self.resources.get("library"):
                self.resources[name] = Path(row["path"])

    def _save(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal.with_name("." + self.journal.name + "." + uuid4().hex)
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": 1, "resources": self._rows}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.journal)
        finally:
            temporary.unlink(missing_ok=True)

    def poll(self, *, force=False):
        # A stalled Windows device call must not hold the status lock or spawn
        # another probe thread on every dashboard request.
        with self._lock:
            if self._probe_thread and self._probe_thread.is_alive():
                if self._probe_timed_out:
                    return self.status()
                event = self._probe_event
            elif not force and time.monotonic() - self._last_probe < 2:
                return self.status()
            else:
                self._last_probe = time.monotonic()
                self._probe_timed_out = False
                event = self._probe_event = threading.Event()
                self._probe_thread = threading.Thread(target=self._probe_cycle, args=(event,),
                                                       name="storage-health", daemon=True)
                self._probe_thread.start()
        if not event.wait(1):
            with self._lock:
                self._probe_timed_out = True
                name = self._probing_name or next(iter(self.resources))
                self._mark_paused(name, "Storage probe has not completed; waiting for the device")
        return self.status()

    def _mark_paused(self, name, message):
        with self._lock:
            row = self._rows.setdefault(name, {"path": str(self.resources[name])})
            if row.get("status") != "PAUSED_STORAGE":
                row.update(paused_at=stamp(), outage_id=uuid4().hex)
            row.update(status="PAUSED_STORAGE", error=message)
            self._successes[name] = 0
            self._last_success[name] = 0
            self._save()

    def _probe_cycle(self, event):
        try:
            with self._lock:
                resources = list(self.resources.items())
                before = json.dumps(self._rows, sort_keys=True)
            for name, path in resources:
                with self._lock:
                    self._probing_name = name
                    row = dict(self._rows.get(name, {"path": str(path), "status": "AVAILABLE"}))
                failure = None
                corrupt = False
                try:
                    if name.startswith("catalog:"):
                        path.stat()
                        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
                        try:
                            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                        finally:
                            connection.close()
                    else:
                        self._probe_directory(name, path, row)
                except (OSError, sqlite3.DatabaseError) as exc:
                    corrupt = ((getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
                    detail = getattr(exc, "strerror", None) or str(exc)
                    failure = f"{path}: {detail}"
                with self._lock:
                    current = self._rows.get(name, row)
                    if failure:
                        self._successes[name] = 0
                        self._last_success[name] = 0
                        if current.get("status") not in {"PAUSED_STORAGE", "RECOVERY_REQUIRED"}:
                            row.update(paused_at=stamp(), outage_id=uuid4().hex)
                        else:
                            row.update({key: current.get(key, "") for key in ("paused_at", "outage_id")})
                        row.update(status="RECOVERY_REQUIRED" if corrupt else "PAUSED_STORAGE", error=failure)
                    elif current.get("status") != "AVAILABLE":
                        now = time.monotonic()
                        if now - self._last_success.get(name, 0) >= 2:
                            self._successes[name] = self._successes.get(name, 0) + 1
                            self._last_success[name] = now
                        row.update({key: current.get(key, "") for key in ("paused_at", "outage_id", "error")})
                        if self._successes.get(name, 0) < 2:
                            row["status"] = "RECOVERING"
                        else:
                            recovered = stamp()
                            history = list(current.get("outages", []))
                            if current.get("outage_id") and (not history or history[-1]["outage_id"] != current["outage_id"]):
                                history.append({"outage_id": current["outage_id"], "paused_at": current.get("paused_at", ""),
                                                "recovered_at": recovered, "error": current.get("error", "")})
                            row.update(status="AVAILABLE", error="", recovered_at=recovered, outages=history[-10:])
                    self._rows[name] = row
            with self._lock:
                if json.dumps(self._rows, sort_keys=True) != before:
                    self._save()
        finally:
            with self._lock:
                self._probing_name = None
                self._probe_timed_out = False
            event.set()

    def _probe_directory(self, name, path, row):
        # A not-yet-created application/queue root can be initialized only
        # before its first successful observation, on an accessible volume.
        target = path
        while name != "library" and not target.exists() and target.parent != target and not row.get("identity"):
            target = target.parent
        stat = target.stat()
        if not target.is_dir():
            raise OSError(f"Storage root is not a directory: {path}")
        identity = [stat.st_dev, stat.st_ino]
        if row.get("identity") and identity != row["identity"]:
            raise OSError(f"Storage identity changed: {path}")
        next(target.iterdir(), None)
        probe = target / (".storage-probe-" + uuid4().hex)
        try:
            with probe.open("xb") as handle:
                handle.write(b"ready")
                handle.flush()
                os.fsync(handle.fileno())
            if probe.read_bytes() != b"ready":
                raise OSError(f"Storage probe could not be read: {path}")
        finally:
            probe.unlink(missing_ok=True)
        if target == path:
            self._learn_identity(name, identity)
            row["identity"] = identity

    def _learn_identity(self, name, identity):
        # First observation is exclusive across dashboard/harvester processes.
        # A stale health snapshot cannot replace an already learned volume/root.
        key = hashlib.sha256((self.journal.name + ":" + name).encode()).hexdigest()
        marker = self.journal.parent / "Identities" / (key + ".json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x", encoding="utf-8") as handle:
                json.dump(identity, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            for attempt in range(3):
                try:
                    saved = json.loads(marker.read_text(encoding="utf-8"))
                    break
                except (json.JSONDecodeError, PermissionError):
                    if attempt == 2:
                        raise StorageUnavailable("Root identity evidence is temporarily unreadable") from None
                    time.sleep(.025)
            if saved != identity:
                raise StorageUnavailable(f"Storage identity changed: {self.resources[name]}")

    def status(self):
        with self._lock:
            return {"available": all(self._rows.get(name, {}).get("status") == "AVAILABLE"
                                     for name in self.resources),
                    "resources": {name: dict(self._rows.get(name, {"status": "UNKNOWN", "path": str(path)}))
                                  for name, path in self.resources.items()}}

    def require(self, *names):
        self.poll()
        with self._lock:
            requested = [(name, self.resources[name], dict(self._rows.get(name, {})))
                         for name in names or self.resources]
        for name, root, row in requested:
            if row.get("status") != "AVAILABLE":
                raise StorageUnavailable(row.get("error") or f"Storage is recovering: {root}")
            try:
                current = root.stat()
            except FileNotFoundError:
                if not row.get("identity") and not name.startswith("catalog:"):
                    continue
                self._mark_paused(name, f"Configured storage disappeared: {root}")
                raise StorageUnavailable(f"Configured storage disappeared: {root}") from None
            except OSError as exc:
                self._mark_paused(name, f"Configured storage cannot be accessed: {root}")
                raise StorageUnavailable(f"Configured storage cannot be accessed: {root}") from exc
            if not name.startswith("catalog:"):
                identity = [current.st_dev, current.st_ino]
                if row.get("identity") and row["identity"] != identity:
                    self._mark_paused(name, f"Storage identity changed: {root}")
                    raise StorageUnavailable(f"Storage identity changed: {root}")
                if not row.get("identity"):
                    self._learn_identity(name, identity)
                    with self._lock:
                        self._rows[name]["identity"] = identity
                        self._save()

    def check_error(self, exc):
        """Return storage attribution only with a failed resource probe."""
        cause = exc
        seen = set()
        while cause is not None:
            if id(cause) in seen:
                break
            seen.add(id(cause))
            if isinstance(cause, StorageUnavailable):
                return True
            if isinstance(cause, sqlite3.Error) and ((getattr(cause, "sqlite_errorcode", 0) or 0) & 0xff) in {sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN}:
                name = "library" if "library" in self.resources else "queue"
                self._mark_paused(name, f"SQLite storage I/O failed; retrying with fresh connections: {cause}")
                return True
            filename = getattr(cause, "filename", None)
            hardware = getattr(cause, "winerror", None) in {21, 55, 64, 67, 1117, 1167} or getattr(cause, "errno", None) in {errno.EIO, errno.ENODEV, errno.ENXIO}
            if isinstance(cause, OSError) and filename and (hardware or (isinstance(cause, FileNotFoundError) and Path(filename).exists())):
                path = Path(filename).resolve()
                with self._lock:
                    for name, root in list(self.resources.items()):
                        if path == root or root in path.parents:
                            self._rows.setdefault(name, {"path": str(root)}).update(
                                status="PAUSED_STORAGE", error=str(cause), paused_at=stamp(), outage_id=uuid4().hex)
                            self._successes[name] = 0
                            self._last_success[name] = 0
                            self._save()
                            return True
            cause = cause.__cause__ or cause.__context__
        if isinstance(exc, (OSError,)) or "database" in type(exc).__name__.lower() or "error" in type(exc).__name__.lower():
            return not self.poll(force=True)["available"]
        return False


