from __future__ import annotations

import json
from pathlib import Path

from file_proxy.reasoning_policy import ReasoningPolicyStore


def write_policy(path: Path, *, operation: str = "remove", value: str | None = None) -> None:
    mapping: dict[str, str] = {"operation": operation, "effective": "template-default"}
    if value is not None:
        mapping["value"] = value
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-09-16T00:00:00+00:00",
                "aliases": {
                    "image-analysis": {
                        "revision_id": "rev-1",
                        "source_digest": "abc",
                        "client_default": "high",
                        "mode": "mapped",
                        "mappings": {
                            "none": {"operation": "remove", "effective": "no-op"},
                            "low": {"operation": "set", "value": "low", "effective": "low"},
                            "medium": {"operation": "set", "value": "medium", "effective": "medium"},
                            "high": mapping,
                        },
                    },
                    "plain": {
                        "revision_id": "rev-2",
                        "source_digest": "def",
                        "client_default": "high",
                        "mode": "unsupported",
                        "mappings": {
                            effort: {"operation": "remove", "effective": "no-op"}
                            for effort in ("none", "low", "medium", "high")
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_policy_applies_default_and_explicit_mappings(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    write_policy(path)
    logs: list[str] = []
    store = ReasoningPolicyStore(path, logs.append)

    omitted = store.normalize(
        "POST", "/v1/chat/completions", b'{"model":"image-analysis:latest","messages":[]}'
    )
    explicit = store.normalize(
        "POST", "/v1/chat/completions",
        b'{"model":"image-analysis","reasoning_effort":"medium","messages":[]}',
    )

    assert omitted.body == b'{"model":"image-analysis:latest","messages":[]}'
    assert omitted.requested == "high"
    assert omitted.operation == "remove"
    assert explicit.body == b'{"model":"image-analysis","reasoning_effort":"medium","messages":[]}'
    assert explicit.operation == "set"
    assert store.status()["alias_count"] == 2
    assert logs


def test_policy_strips_recognized_effort_for_unsupported_model(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    write_policy(path)
    store = ReasoningPolicyStore(path, lambda _message: None)

    result = store.normalize(
        "POST", "/v1/chat/completions",
        b'{"model":"plain:latest","reasoning_effort":"high","messages":[]}',
    )

    assert result.body == b'{"model":"plain:latest","messages":[]}'
    assert result.effective == "no-op"


def test_policy_preserves_unknown_and_native_requests(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    write_policy(path)
    store = ReasoningPolicyStore(path, lambda _message: None)
    unknown_alias = b'{"model":"other","reasoning_effort":"high"}'
    unknown_effort = b'{"model":"image-analysis","reasoning_effort":"ultra"}'
    native = b'{"model":"image-analysis","think":"high"}'

    assert store.normalize("POST", "/v1/chat/completions", unknown_alias).body is unknown_alias
    assert store.normalize("POST", "/v1/chat/completions", unknown_effort).body is unknown_effort
    assert store.normalize("POST", "/api/chat", native).body is native


def test_policy_hot_reload_retains_last_known_good(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    logs: list[str] = []
    write_policy(path)
    store = ReasoningPolicyStore(path, logs.append)
    store.refresh()
    write_policy(path, operation="set", value="max")

    updated = store.normalize(
        "POST", "/v1/chat/completions", b'{"model":"image-analysis","messages":[]}'
    )
    assert json.loads(updated.body)["reasoning_effort"] == "max"

    path.write_text("{", encoding="utf-8")
    retained = store.normalize(
        "POST", "/v1/chat/completions", b'{"model":"image-analysis","messages":[]}'
    )
    assert json.loads(retained.body)["reasoning_effort"] == "max"
    assert store.status()["last_known_good"] is True
    assert store.status()["last_reload_error"]
