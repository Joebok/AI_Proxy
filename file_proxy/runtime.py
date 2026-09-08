from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ManagedComfyUIConfig:
    enabled: bool = False
    executable: str | None = None
    script: str | None = None
    arguments: tuple[str, ...] = ()
    working_directory: str | None = None
    model_config: str | None = None
    upstream: str = "http://127.0.0.1:8188"
    startup_deadline_seconds: float = 180.0
    startup_cooldown_seconds: float = 30.0
    idle_shutdown_seconds: float = 60.0
    shutdown_deadline_seconds: float = 15.0
    log_max_bytes: int = 4 * 1024 * 1024
    log_backups: int = 3


@dataclass(frozen=True)
class RuntimeConfig:
    runtime_dir: str = ".ai-proxy-runtime"
    managed_comfyui: ManagedComfyUIConfig = field(default_factory=ManagedComfyUIConfig)
    generation_settlement_seconds: float = 7500.0
    cancellation_grace_seconds: float = 30.0
    queue_wait_seconds: float = 1800.0
    max_admitted_requests: int = 32
    max_buffered_body_bytes: int = 256 * 1024 * 1024
    retention_days: int = 7
    cache_max_bytes: int = 10 * 1024 * 1024 * 1024
    max_resource_streak: int = 5
    oldest_request_seconds: float = 60.0

    @classmethod
    def load(cls, path: Path | None) -> "RuntimeConfig":
        if path is None:
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid runtime configuration {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("runtime configuration must contain an object")
        known = {item.name for item in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown runtime configuration setting(s): {', '.join(sorted(unknown))}")
        managed = data.pop("managed_comfyui", {})
        if not isinstance(managed, dict):
            raise ValueError("managed_comfyui must contain an object")
        managed_known = {item.name for item in fields(ManagedComfyUIConfig)}
        managed_unknown = set(managed) - managed_known
        if managed_unknown:
            raise ValueError(f"unknown managed_comfyui setting(s): {', '.join(sorted(managed_unknown))}")
        if "arguments" in managed:
            args = managed["arguments"]
            if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
                raise ValueError("managed_comfyui.arguments must be a string array")
            managed["arguments"] = tuple(args)
        result = cls(managed_comfyui=ManagedComfyUIConfig(**managed), **data)
        result.validate(path.parent)
        return result

    def resolved(self, base: Path) -> "RuntimeConfig":
        runtime_dir = Path(self.runtime_dir)
        def absolute(value: str | None) -> str | None:
            if not value:
                return value
            path = Path(value)
            return str(path if path.is_absolute() else (base / path).resolve())
        managed = replace(
            self.managed_comfyui,
            executable=absolute(self.managed_comfyui.executable),
            script=absolute(self.managed_comfyui.script),
            working_directory=absolute(self.managed_comfyui.working_directory),
            model_config=absolute(self.managed_comfyui.model_config),
        )
        return replace(
            self,
            runtime_dir=str(runtime_dir if runtime_dir.is_absolute() else (base / runtime_dir).resolve()),
            managed_comfyui=managed,
        )

    def validate(self, base: Path | None = None) -> None:
        positive = {
            "generation_settlement_seconds": self.generation_settlement_seconds,
            "cancellation_grace_seconds": self.cancellation_grace_seconds,
            "queue_wait_seconds": self.queue_wait_seconds,
            "max_admitted_requests": self.max_admitted_requests,
            "max_buffered_body_bytes": self.max_buffered_body_bytes,
            "retention_days": self.retention_days,
            "cache_max_bytes": self.cache_max_bytes,
            "max_resource_streak": self.max_resource_streak,
        }
        if any(isinstance(value, bool) or value <= 0 for value in positive.values()):
            raise ValueError("runtime limits must be positive")
        managed = self.managed_comfyui
        if managed.enabled and (not managed.executable or not managed.working_directory):
            raise ValueError("managed ComfyUI requires executable and working_directory")
        if managed.enabled:
            parsed = urlsplit(managed.upstream)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("managed ComfyUI upstream must be a loopback HTTP address")
            root = base or Path.cwd()
            executable = Path(managed.executable)
            working = Path(managed.working_directory)
            if not executable.is_absolute():
                executable = root / executable
            if not working.is_absolute():
                working = root / working
            if not executable.is_file():
                raise ValueError(f"managed ComfyUI executable does not exist: {executable}")
            if not working.is_dir():
                raise ValueError(f"managed ComfyUI working directory does not exist: {working}")
            for optional in (managed.script, managed.model_config):
                if optional:
                    candidate = Path(optional)
                    if not candidate.is_absolute():
                        candidate = root / candidate
                    if not candidate.is_file():
                        raise ValueError(f"managed ComfyUI file does not exist: {candidate}")
            for flag in ("--input-directory", "--output-directory"):
                if flag in managed.arguments:
                    index = managed.arguments.index(flag)
                    if index + 1 >= len(managed.arguments):
                        raise ValueError(f"{flag} requires a path")
                    directory = Path(managed.arguments[index + 1])
                    if not directory.is_absolute():
                        directory = root / directory
                    if not directory.is_dir():
                        raise ValueError(f"managed ComfyUI directory does not exist: {directory}")


def merge_cli(config: RuntimeConfig, **values: Any) -> RuntimeConfig:
    """Apply only explicitly supplied CLI values to a loaded config."""
    return replace(config, **{key: value for key, value in values.items() if value is not None})
