# HTTP forwarding, prompt settlement, and caching

## Responsibility and interfaces

[http_proxy.py](../../file_proxy/http_proxy.py) owns `HttpProxyConfig`, backend
profiles, admission, `HttpProxyService`, relay/prompt lifecycles, and
`run_http_proxies`. It consumes the shared [scheduler](SCHEDULING.md), optional
`ExecutionStore`, `BackendManager`, diagnostic log and reasoning-policy store.
Listeners proxy configured upstreams; no conversation memory or task prompt
generation is implemented. Formats/limits/persistence are owned by
[DATA_MODEL.md](../DATA_MODEL.md).

| Route family | Dispatch behavior |
| --- | --- |
| Ollama | Queue everything except configured bypass routes; defaults are GET `/api/ps`, `/api/tags`, `/api/version`, `/v1/models`. Known inference POST routes derive `ollama:<model>` keys. |
| External ComfyUI | Queue routes in `COMFYUI_QUEUE_ROUTES`: native and `/api` prompt, queue/history mutations, listed `sdapi` image endpoints. Others bypass. |
| Managed ComfyUI | Queue ordinary discovery/upload routes too because they may need process startup. `/interrupt` aliases, `/ws` and `/_proxy/*` excluded from queue classification. |
| Special proxy/cache routes | Handled before classification/admission: GET `/_proxy/status`, `/_proxy/jobs/<request_id>`, cached keyed history and `/view`; managed aggregate history serves local completed records. Cache misses continue to normal classification/forwarding. |

The listener does not implement WebSocket bridging; `/ws` exclusion is not a
working transparent WebSocket tunnel because relay strips upgrade headers and
uses ordinary HTTP. Progress clients should use raw ComfyUI `/ws`.

## Admission and ordinary relay

`handle` resolves special routes first; bypasses read bodies without shared
inference admission. Queued routes reserve request capacity, read chunks with a
per-request limit and shared buffered-byte accounting, optionally normalize the
Ollama effort field, then enqueue. Queue/history POST mutations set a barrier.
Queue wait expires independently of upstream execution timeout. Failures:
413 per-body overflow; 503 admission/shutdown/backend errors (capacity/backend
errors add Retry-After); 504 queue wait/upstream timeout; 502 upstream transport.

`_forward_headers` strips hop-by-hop headers and Connection-nominated headers;
request Host is replaced by upstream handling. `_relay` preserves method/raw path,
upstream status and ordinary body bytes, uses `allow_redirects=False`, and streams
64 KiB chunks with response auto-decompression disabled. NDJSON/SSE are forwarded
as bytes, not parsed. It adds `X-AI-Proxy-Request-ID` for queued relays. No rewriting
of `keep_alive` or client message content; effort policy is the narrow exception.

Ollama known inference routes create durable records at dispatch and emit body-free
diagnostic events. `completed` describes transport completion, not verified model
output correctness; ordinary relay can forward a non-2xx upstream response.
Timeout/transport failures after dispatch may unload/reconcile Ollama and block
the manager if ambiguity remains. The current `_relay` cancellation branch
attempts to drain remaining Ollama stream bytes after a downstream disconnect;
that attempt is not a guaranteed drain because request context cancellation can
close upstream. The disconnect test observes a cancelled outcome. Preserve this
distinction when changing stream ownership.

## ComfyUI prompt lifecycle

Settle routes are `POST /prompt` and `/api/prompt`. `handle` transfers ownership
to a tracked `_run_prompt_lifecycle` task before upstream dispatch. Client loss
before dispatch cancels waiting work; after dispatch the task still reconciles.

After `started`, the task requires a workflow object, injects a canonical-form UUID
when absent or validates supplied ID, and creates durable unique tracking before
dispatch. A valid returned different upstream ID replaces the tracked ID unless
already retained. It reads bounded upstream response, decodes gzip/deflate where
needed, recalculates response headers, and delivers submission response immediately.

The slot remains held after response delivery. One monotonic settlement deadline
starts before upstream dispatch; `_wait_for_prompt_completion` polls native or
`/api/history/<id>` within remaining time. A parsed dict under that exact ID is
treated as terminal (it does not require a successful status). Archive separately
requires `status.completed is True` and `status_str` success/succeeded for success.

At expiry `_cancel_and_reconcile` requests interruption and polls for cancellation
grace. If still unresolved, it stops only an owned managed backend and records
failure; external unresolved generation records blocked outcome and awaits forever
until shutdown cancels the lifecycle. `finally` releases admission and finish;
never use response close alone as the slot-release condition.

## Archival and restart

`_archive_completion` evicts eligible old data, extracts standard filename records
from output node lists (`output`/`temp` only), fetches each via `/view`, writes a
bounded `.partial` file, renames it, registers metadata, and only then stores history.
Successful history may delete original managed output duplicates: only with
proxy-owned enabled backend, nonempty cached file, and a resolved file beneath
configured `--output-directory`. External originals and `temp` files are not
deleted by that cleanup path. This differs from the retained older integration note.

Archive errors record `archive_failed` and cache error; failed upstream history
can still be archived and retrieved. Cache-backed keyed history/view and durable
job status survive backend stop/restart while retained. See store methods before
changing history clearing, lookup without prompt ID, or eviction.

Runtime startup reconciles abandoned Ollama records by verified unload; it never
replays requests. ComfyUI pending records are checked briefly for keyed history:
archive when found, fail in managed mode if the previous owned backend ended,
or block external ambiguity. Inspect `reconcile_records` and its chosen service
upstream before changing listener combinations; behavior with missing listeners
does not establish complete restart reconciliation for all configurations.

## Extension and verification

New routes need a deliberate choice of queue/bypass/settlement/barrier, resource
identity, admission ownership, and durable record behavior. New artifact formats
need archive/cache safety and publication-order checks. Profile defaults and CLI
configuration must stay consistent across headless/dashboard modes.

[test_http_proxy.py](../../tests/test_http_proxy.py) covers streaming incrementality,
headers/body forwarding, bypass, body limits, upstream timeout/failure, resource
keys and disconnect diagnostics. [test_reliable_lifecycle.py](../../tests/test_reliable_lifecycle.py)
covers UUID tracking, terminal error, keyed-history slot holding, store reopen and
managed-output path containment. Full archival/retention/delete lifecycle and
external unresolved cancellation/restart acceptance are not fully covered.

Related: [INVARIANTS.md](../INVARIANTS.md),
[execution ownership ADR](../DECISIONS/0002-shared-execution-and-backend-ownership.md),
[managed runtime](MANAGED_RUNTIME.md), [reasoning policy](REASONING_POLICY.md).
