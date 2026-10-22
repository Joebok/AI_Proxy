from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable


RECOGNIZED_EFFORTS = frozenset({"none", "low", "medium", "high"})
OPERATIONS = frozenset({"set", "remove"})


@dataclass(frozen=True)
class NormalizationResult:
    body: bytes
    alias: str | None = None
    requested: str | None = None
    operation: str | None = None
    effective: str | None = None
    revision_id: str | None = None


class ReasoningPolicyStore:
    """Hot-reloaded, last-known-good alias reasoning policies."""

    def __init__(self, path: str | Path | None, logger: Callable[[str], None]) -> None:
        self.path = Path(path).resolve() if path else None
        self.logger = logger
        self._aliases: dict[str, dict[str, Any]] = {}
        self._mtime_ns: int | None = None
        self._loaded_at: str | None = None
        self._generated_at: str | None = None
        self._last_error: str | None = None
        self._has_loaded = False

    def refresh(self) -> None:
        if self.path is None:
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._record_error(f"cannot read reasoning policy {self.path}: {exc}")
            return
        if self._mtime_ns == stat.st_mtime_ns:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            aliases, generated_at = self._validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self._record_error(f"invalid reasoning policy {self.path}: {exc}")
            return
        self._aliases = aliases
        self._mtime_ns = stat.st_mtime_ns
        self._loaded_at = datetime.now(timezone.utc).isoformat()
        self._generated_at = generated_at
        self._last_error = None
        self._has_loaded = True
        self.logger(f"Loaded reasoning policy for {len(aliases)} alias(es) from {self.path}")

    def normalize(self, method: str, path: str, body: bytes) -> NormalizationResult:
        if (method, path) != ("POST", "/v1/chat/completions"):
            return NormalizationResult(body)
        self.refresh()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return NormalizationResult(body)
        if not isinstance(payload, dict):
            return NormalizationResult(body)
        model = str(payload.get("model") or "").strip().casefold()
        alias = model.removesuffix(":latest")
        policy = self._aliases.get(alias)
        if not policy:
            return NormalizationResult(body)

        explicit = "reasoning_effort" in payload
        requested = payload.get("reasoning_effort") if explicit else policy["client_default"]
        if not isinstance(requested, str) or requested not in RECOGNIZED_EFFORTS:
            return NormalizationResult(body)
        mapping = policy["mappings"].get(requested)
        if not mapping:
            return NormalizationResult(body)

        operation = mapping["operation"]
        if operation == "remove":
            payload.pop("reasoning_effort", None)
        else:
            payload["reasoning_effort"] = mapping["value"]
        normalized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if normalized == body:
            normalized = body
        return NormalizationResult(
            normalized,
            alias=alias,
            requested=requested,
            operation=operation,
            effective=str(mapping.get("effective") or ""),
            revision_id=str(policy.get("revision_id") or ""),
        )

    def status(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "loaded_at": self._loaded_at,
            "generated_at": self._generated_at,
            "alias_count": len(self._aliases),
            "last_reload_error": self._last_error,
            "last_known_good": self._has_loaded,
        }

    def _record_error(self, message: str) -> None:
        if message != self._last_error:
            self.logger(message)
        self._last_error = message

    @staticmethod
    def _validate(payload: object) -> tuple[dict[str, dict[str, Any]], str | None]:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("schema_version must be 1")
        raw_aliases = payload.get("aliases")
        if not isinstance(raw_aliases, dict):
            raise ValueError("aliases must be an object")
        aliases: dict[str, dict[str, Any]] = {}
        for raw_name, raw_policy in raw_aliases.items():
            name = str(raw_name).strip().casefold()
            if not name or not isinstance(raw_policy, dict):
                raise ValueError("each alias policy must be an object")
            default = raw_policy.get("client_default")
            mode = raw_policy.get("mode")
            mappings = raw_policy.get("mappings")
            if default not in RECOGNIZED_EFFORTS:
                raise ValueError(f"{name}.client_default is invalid")
            if mode not in {"mapped", "unsupported"}:
                raise ValueError(f"{name}.mode is invalid")
            if not isinstance(mappings, dict):
                raise ValueError(f"{name}.mappings must be an object")
            validated: dict[str, dict[str, Any]] = {}
            for effort, mapping in mappings.items():
                if effort not in RECOGNIZED_EFFORTS or not isinstance(mapping, dict):
                    raise ValueError(f"{name} has an invalid effort mapping")
                operation = mapping.get("operation")
                if operation not in OPERATIONS:
                    raise ValueError(f"{name}.{effort}.operation is invalid")
                if operation == "set" and not isinstance(mapping.get("value"), str):
                    raise ValueError(f"{name}.{effort}.value is required for set")
                validated[effort] = dict(mapping)
            if default not in validated:
                raise ValueError(f"{name} has no mapping for its default")
            if mode == "unsupported" and any(
                mapping["operation"] != "remove" for mapping in validated.values()
            ):
                raise ValueError(f"{name} unsupported mappings must remove reasoning fields")
            aliases[name] = {**raw_policy, "mappings": validated}
        generated_at = payload.get("generated_at")
        return aliases, str(generated_at) if generated_at else None
