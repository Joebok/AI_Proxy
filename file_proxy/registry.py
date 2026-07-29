from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .models import WorkerRegistration, validate_identifier


class Registry:
    def __init__(self, subscribers: dict[str, dict[str, WorkerRegistration]]):
        self.subscribers = subscribers

    @classmethod
    def load(cls, registry_dir: Path) -> "Registry":
        subscribers: dict[str, dict[str, WorkerRegistration]] = {}
        if not registry_dir.is_dir():
            raise ValueError(f"registry directory does not exist: {registry_dir}")
        for path in sorted(registry_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid registry file {path}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"registry file must contain an object: {path}")
            subscriber_id = validate_identifier(data.get("subscriber_id", path.stem), "subscriber_id")
            workers_data = data.get("workers")
            if not isinstance(workers_data, dict) or not workers_data:
                raise ValueError(f"registry has no workers: {path}")
            if subscriber_id in subscribers:
                raise ValueError(f"duplicate subscriber registry: {subscriber_id}")
            expanded_workers = {}
            for name, registration in workers_data.items():
                if not isinstance(registration, dict):
                    raise ValueError(f"invalid worker registration {name!r}: {path}")
                expanded = {
                    key: cls._expand(value, path)
                    for key, value in registration.items()
                }
                worker = WorkerRegistration.from_dict(expanded)
                if worker.working_directory and not Path(worker.working_directory).is_dir():
                    raise ValueError(
                        f"worker working_directory does not exist for {name!r}: "
                        f"{worker.working_directory}"
                    )
                expanded_workers[validate_identifier(name, "worker")] = worker
            subscribers[subscriber_id] = expanded_workers
        return cls(subscribers)

    @classmethod
    def _expand(cls, value, path: Path):
        if isinstance(value, list):
            return [cls._expand(item, path) for item in value]
        if not isinstance(value, str):
            return value

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                raise ValueError(f"registry {path} requires environment variable {name}")
            return resolved

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)

    def worker(self, subscriber_id: str, worker: str) -> WorkerRegistration | None:
        return self.subscribers.get(subscriber_id, {}).get(worker)

    def validate_subscriber(self, subscriber_id: str) -> dict[str, object]:
        workers = self.subscribers.get(subscriber_id)
        if workers is None:
            raise ValueError(f"unknown subscriber: {subscriber_id}")
        return {"subscriber_id": subscriber_id, "workers": sorted(workers)}
