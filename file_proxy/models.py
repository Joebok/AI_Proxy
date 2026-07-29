from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any


PROTOCOL_VERSION = 1
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InvalidJob(ValueError):
    pass


class JobNotReady(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise InvalidJob("created_at must be a UTC ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidJob("created_at must be a UTC ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise InvalidJob("created_at must include the UTC offset")
    return parsed.astimezone(timezone.utc)


def validate_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise InvalidJob(f"{name} is invalid")
    return value


@dataclass(frozen=True)
class JobManifest:
    protocol_version: int
    job_id: str
    subscriber_id: str
    worker: str
    created_at: datetime
    files: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> "JobManifest":
        if not isinstance(data, dict):
            raise InvalidJob("job.json must contain an object")
        if data.get("protocol_version") != PROTOCOL_VERSION:
            raise InvalidJob(f"protocol_version must be {PROTOCOL_VERSION}")
        files = data.get("files", [])
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise InvalidJob("files must be an array of objects")
        return cls(
            protocol_version=PROTOCOL_VERSION,
            job_id=validate_identifier(data.get("job_id"), "job_id"),
            subscriber_id=validate_identifier(data.get("subscriber_id"), "subscriber_id"),
            worker=validate_identifier(data.get("worker"), "worker"),
            created_at=parse_utc(data.get("created_at")),
            files=tuple(files),
        )


@dataclass(frozen=True)
class WorkerRegistration:
    command: tuple[str, ...]
    timeout_seconds: float
    working_directory: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "WorkerRegistration":
        if not isinstance(data, dict):
            raise ValueError("worker registration must be an object")
        command = data.get("command")
        timeout = data.get("timeout_seconds", data.get("timeout"))
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ValueError("worker command must be a non-empty string array")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("worker timeout_seconds must be positive")
        working_directory = data.get("working_directory")
        if working_directory is not None and (
            not isinstance(working_directory, str) or not working_directory
        ):
            raise ValueError("worker working_directory must be a non-empty string")
        return cls(tuple(command), float(timeout), working_directory)


@dataclass
class ProxyResult:
    protocol_version: int
    job_id: str
    subscriber_id: str
    worker: str
    status: str
    started_at: str
    completed_at: str
    duration_seconds: float
    exit_code: int | None
    error_type: str | None = None
    error_message: str | None = None
    stdout: str = ""
    stderr: str = ""
    output_files: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueueJob:
    path: Path
    manifest: JobManifest
