# Alias reasoning-effort normalization

## Responsibility and boundary

[reasoning_policy.py](../../file_proxy/reasoning_policy.py) `ReasoningPolicyStore`
loads optional external alias metadata and maps one field on queued Ollama
`POST /v1/chat/completions`. It is called from `HttpProxyService.handle` and
refreshed by Ollama `/_proxy/status` in [http_proxy.py](../../file_proxy/http_proxy.py).
Config supplies `ollama_reasoning_policy_path`; no path means no policy.

The store does not generate/merge system prompts, rewrite messages, select task
roles, or rewrite native Ollama `think`. Subscriber/client owns task content.
The existing ModelUpdater ownership note indicates external alias metadata intent;
this task did not inspect the generating repository or its deployed policy.

## Format and normalization

Authoritative parser: `ReasoningPolicyStore._validate`; compact examples:
[test_reasoning_policy.py](../../tests/test_reasoning_policy.py) `write_policy`.
Root: `schema_version: 1`, `aliases` object, optional `generated_at`.
Each alias policy requires:

- `client_default` in `none`, `low`, `medium`, `high`.
- `mode`: `mapped` or `unsupported`.
- `mappings` keyed by recognized efforts, including the default.
- Each mapping has `operation` `set` (string `value` required) or `remove`.
  Unsupported mode requires removal mappings. Optional `effective` and
  `revision_id` describe logged metadata; other metadata may be preserved.

On the supported route, parse a JSON object and normalize model to trimmed,
casefolded alias with trailing `:latest` removed. Unknown alias, invalid JSON,
unrecognized effort, missing mapping or other routes return original bytes.
Omitted effort uses policy default; explicit recognized effort selects its mapping.
`set` writes `reasoning_effort`; `remove` deletes it. A set value need only be a
string: e.g. source effort `high` can map to upstream value `max`.

Only changed/recognized mapping paths reserialize compact UTF-8 JSON; other
payload semantics remain untouched. HTTP relay recalculates Content-Length when
body length changes. Normalization logs alias/effort/operation/effective/revision,
not messages or body. Consumers of byte-identical payloads must account for the
documented normalization exception.

## Reload, failure, extension, verification

`refresh` compares `st_mtime_ns` with the last successfully loaded value. It
atomically replaces in-memory aliases after a fully valid read. Missing/malformed
files record a deduplicated error and retain the last good policy; before first
successful load normalization remains inactive. It does not persist policy copies.
Status reports source path, loaded/generated time, alias count, last reload error
and whether any last-known-good load exists.

Known observation: `test_policy_hot_reload_retains_last_known_good` failed twice
on rapid rewrite during this task; timestamp-only detection is a suspected cause,
not a confirmed diagnosis. Do not mark hot reload reliably verified on this host.
Also, `datetime.UTC` import leaves the packaging Python 3.10 minimum unverified.
See [DEVELOPMENT.md](../DEVELOPMENT.md).

Extension points: new recognized efforts, policy versions or mapping fields need
format validation, native/unknown preservation checks and external producer
compatibility analysis. Expanding supported routes changes transport behavior and
requires a plan; it must not introduce task-specific prompt interpretation.

Tests cover explicit/default mapping, unsupported removal, unknown/native route
preservation and intended last-known-good reload. Missing-file recovery and
header/admission effects of normalization are not independently established by
dedicated integration tests. Related boundary:
[transport ADR](../DECISIONS/0001-transport-and-local-state.md) and
[INVARIANTS.md](../INVARIANTS.md).
