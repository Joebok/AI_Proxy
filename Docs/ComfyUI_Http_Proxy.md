 # ComfyUI HTTP Proxy — Implementation Plan

   ## 1. Goal

   Add a ComfyUI HTTP proxy to File Job Proxy, modeled on the existing Ollama proxy, so that ComfyUI image generations
 are serialized with Ollama inference and filesystem jobs through the same shared queue and scheduler:

   - One machine, one GPU. Ollama, ComfyUI-Forge, and filesystem jobs must not run concurrently.
   - ComfyUI requests flow through `POST /prompt` (native) or `/sdapi/v1/*` (API) and return **immediately** while the
 actual generation runs for seconds to minutes. The proxy must hold the GPU slot for that whole window, not just until
 the HTTP response closes.
   - Read-only/non-destructive routes (`GET /history`, `/view`, `/upload/*`, `GET /queue`, `/interrupt`, `/outputs`,
 …) bypass the queue so clients can poll/inspect/upload or interrupt while a generation is in flight. Destructive
 history/queue mutations are serialized so they cannot erase the evidence used to settle a tracked prompt.
   - WebSocket (`/ws`) is **not** proxied. Clients point websockets and image/history fetches directly at the upstream
 ComfyUI server.

   ## 2. Validation against a real job run

   Reviewed `Zet_Library/Pipelines/Single_Character_Lab/20260823_194607_979608`:

   - 6-seed character render (Tsaeytte, Elder), ~22 s per image, native ComfyUI API.
   - Workflow uses `CheckpointLoaderSimple` with a `ckpt_name` field → this is the resource key the proxy derives
 (`comfyui:<ckpt_name>`), which plugs directly into the existing resource-streak scheduler and the Forge
 `unload-checkpoint` flow in `engine.py` (no engine changes needed: any non-`ollama:` incoming key after a non-ollama
 resource is fine; `ollama:` after `comfyui:` already triggers unload via the existing "previous key doesn't start with
 ollama" check — in fact the engine only special-cases *entering* ollama from non-ollama, which is exactly the
 comfyui→ollama transition we want).
   - Confirmed the three behaviors that matter: uploads must bypass; `POST /prompt` must hold the slot for the full ~22
 s generation; `/history` and `/view` must bypass so polling works while the slot is held.

   ## 3. Architecture / design decisions

   ### 3.1 Backend profile abstraction (decision — made)

   `http_proxy.py` is generalized around a per-backend **profile** instead of hard-coded Ollama route tables. This is
 required because the two backends have *inverse* route models:

   | Policy | Ollama | ComfyUI |
   |---|---|---|
   | Default action | **queue** (all routes except bypass list) | **bypass** (all routes except queue list) |
   | Routing tables | `bypass_routes` (small) | `queue_routes` + `settle_routes` (small) |
   | Settle condition | HTTP response EOF (streaming) | Parsed terminal history entry exists for the submitted
 `prompt_id` |
   | Resource key | `ollama:<model>` from `model` field | `comfyui:<ckpt_name>` from workflow nodes |

 ```

 BackendProfile (frozen dataclass):
   name: "ollama" | "comfyui"
   queue_all_except_bypass: bool
   queue_routes: frozenset[(method, path)]
   settle_routes: frozenset[(method, path)]
   resource_key_fn: method, path, body -> str | None

 ```

   - `OLLAMA_PROFILE` = existing behavior (queue-all-except-configured-bypass, settle on EOF,
 `request_resource_key`).
   - `COMFYUI_PROFILE` = queue-only-listed, settle `POST /prompt` and `POST /api/prompt` via history poll,
 `comfyui_resource_key`.
   - `HttpProxyConfig` gains `profile` (default `OLLAMA_PROFILE`) and `settle_poll_seconds` (default 1.0), both
 appended **after** existing fields with defaults → all existing call sites (`tests/test_http_proxy.py`, `cli.py`) keep
 working unchanged.
   - `HttpProxyConfig.bypass_routes` remains the single Ollama bypass table, preserving
 `--http-bypass-route`. It is consulted only when `profile.queue_all_except_bypass` is true; profiles do not carry a
 second bypass table.

   ### 3.2 ComfyUI route tables (decision — made)

   **Queued (hold slot):**
   - `POST /prompt` (native API; the real trigger)
   - `POST /api/prompt` (the current ComfyUI `/api` alias)
   - `POST /sdapi/v1/txt2img`, `POST /sdapi/v1/img2img`, `POST /sdapi/v1/extrapolate`, `POST /sdapi/v1/upscale`
 (SD-WebUI-style API; harmless to list even when Forge doesn't implement them — they'll just 404 at upstream if absent)
   - `POST /queue`, `POST /api/queue`, `POST /history`, and `POST /api/history` are also serialized control routes.
 They settle on response EOF and intentionally wait behind an active prompt so clearing/deleting queue or history state
 cannot strand its settle watchdog. `POST /interrupt` remains a bypass route so active work can be stopped.

   **Settle routes** (hold slot *until completion* rather than until response EOF):
   - `("POST", "/prompt")` and `("POST", "/api/prompt")`.
   - Rationale: the native `/prompt` response is tiny (`{"prompt_id": ..., "number": N}`) and returns while the GPU is
 still busy — the single most important case.
   - The `/sdapi/v1/*` routes settle on response EOF (their responses are blocking/streaming and complete only when the
 job is done, so EOF already means "GPU free").

   **Bypass (everything else — the default in queue-only-listed policy):** `GET /history`, `GET /prompt`, `GET /queue`,
 `POST /interrupt`, `POST /backpressure`, `GET /view`, `GET /outputs`, `POST /upload/image`, `POST /upload/mask`,
 `GET /system_stats`, `GET /object_info`, `GET /stats`, `POST /prompt/executors`, `GET /experiment/...`, their `/api`
 aliases where ComfyUI provides them, and `/ws` (clients connect directly), …

   ### 3.3 Settle mechanism — history polling (decision — made, with rationale)

   Options considered:

   1. **Release on response EOF** (current Ollama model) — *rejected for `/prompt`*: response returns in ~ms while
 generation takes ~22 s; slot would be released immediately, defeating the purpose.
   2. **Track via `/ws`** — *rejected*: not proxying websockets (see 3.6); also more state, and Forge/ComfyUI ws
 protocol is version-sensitive.
   3. **Poll `GET /history/{prompt_id}`** — **chosen**. For a submitted `/api/prompt`, poll
 `GET /api/history/{prompt_id}` instead. Poll every `settle_poll_seconds` (default 1.0 s), parse JSON, and settle only
 when the payload is a dict whose `payload[prompt_id]` is a dict. Raw body length is not a valid test because `{}` is a
 non-empty byte string. Any returned history entry is terminal, including error/interrupted prompts, so the GPU is
 free. Wrap the entire polling loop in one explicit monotonic/`asyncio.timeout()` deadline of
 `upstream_timeout_seconds` (default 7500 s ≈ 2 h); the `aiohttp` per-request timeout alone does not bound the loop.
 Log a warning on deadline expiry.
      - Before dispatching a valid native prompt payload, preserve a valid caller-supplied `prompt_id` or generate a
 UUID and add it to the forwarded JSON. Current ComfyUI accepts a caller-supplied prompt ID, so the lifecycle task has
 a tracking key before the non-idempotent request leaves the proxy. Strip the original `Content-Length` when the JSON
 body is rewritten and let `aiohttp` calculate the new value. If a successful response returns a different valid ID,
 log a warning and track the returned ID.
      - A non-2xx validation response releases immediately. If transport fails after dispatch, keep reconciling the
 known prompt ID through history until it settles or the explicit deadline expires; the client may receive 502/504,
 but ambiguous delivery must not permit concurrent GPU work.

   ### 3.4 Slot ownership must survive client disconnect (decision — made; subtle)

   `aiohttp` server is run with `handler_cancellation=True`, so if the client closes its connection after receiving the
 small `/prompt` response, the *handler task* is cancelled. If slot release were tied to handler `finally:` (as it is
 today for Ollama), the slot would be released the moment a well-behaved client closes.

   Fix: for settle routes, the handler hands the job to a tracked, service-owned **prompt lifecycle task before any
 upstream dispatch**. The lifecycle task, not the request handler, owns completion of the queue job:

   - `HttpProxyService` keeps `self._lifecycle_tasks: set[asyncio.Task]`.
   - After `job.started`, `handle()` creates and registers `_run_prompt_lifecycle(job, request_data)` and sets
 `handed_off=True` **before** that task sends the request upstream. The handler then awaits a shielded response future
 owned by the lifecycle task. Handler cancellation cannot cancel the lifecycle task.
   - `_run_prompt_lifecycle` sends the upstream request, buffers the small response, publishes status/headers/body to
 the handler's response future, extracts `prompt_id`, and polls history per §3.3. It calls `queue.finish(job)` in its
 own `finally` block.
   - The handler writes the buffered response only after ownership has transferred. Disconnect during response
 delivery therefore cannot release the slot.
   - If the client disconnects after upstream dispatch but before an upstream response is available, the lifecycle
 task continues independently and settles the known prompt ID. An ambiguous upstream transport failure returns the
 existing 502/504 downstream when possible but retains the slot while reconciling that ID through history.
   - `stop()` first closes the shared queue to reject/wake waiting work, then cancels and gathers lifecycle tasks; each
 task's `finally` resolves its active job before the shared HTTP session is closed.

   Net effect: from the start of upstream dispatch through terminal history, no other queued HTTP job *and* no
 filesystem job is started (scheduler is blocked on
 `http_job.finished`), so the GPU slot is genuinely held for the full generation — exactly the serialization we want.

   ### 3.5 Resource key extraction (decision — made)

   `comfyui_resource_key(method, path, body)`:

   - Returns `None` unless `(method, path)` is in the ComfyUI queue table (so bypass routes never inject affinity —
 same pattern as Ollama's `MODEL_ROUTES` gate).
   - Parse body as JSON. For native `/prompt` and `/api/prompt`, inspect `payload["prompt"]` as the workflow.
   - Walk workflow node `dict` values; for each node with an `inputs` dict, read `ckpt_name` (covers
 `CheckpointLoaderSimple`, `CheckpointLoader`, SD-WebUI-Compat `CheckpointLoader`); first non-empty string wins →
 `comfyui:<ckpt_name>`.
   - SD-WebUI-style request bodies are not ComfyUI workflow objects and receive no resource key unless a separately
 verified Forge payload mapping is added. They still queue correctly with `None` affinity.
   - Never fails: invalid JSON / missing field → `None` (job still queues, just without an affinity key).

   No engine changes needed: `engine.py::_prepare_resource` already triggers Forge `sdapi/v1/unload-checkpoint`
 whenever the incoming key is `ollama:` and the prior one isn't; `comfyui:` keys flow through the streak scheduler as
 opaque strings.

   ### 3.6 WebSocket (decision — made, from earlier discussion)

   **Do not proxy `/ws`.** Clients must point their websocket and `/view`/`/history`/`/uploads` traffic directly at the
 upstream ComfyUI. Proxying ws through a queued GPU slot is disproportionately complex (framing, ping-pong, lifetime
 tied to an unbounded interactive session) for zero benefit — the ws channel carries *progress*, and a direct
 connection gives that natively. Document the client contract in README: queue endpoint = `:PORT`, progress endpoint =
 `http://127.0.0.1:8188`.

   ### 3.7 Multi-listener runner (decision — made)

   - New `run_http_proxies(proxy, configs: list[HttpProxyConfig], poll_seconds, http_logger)` — one `HttpQueue`, one
 `Proxy`/scheduler, N `HttpProxyService` instances (one listen socket per backend). Both backends share the same queue
 so ComfyUI and Ollama jobs serialize against each other *and* against filesystem jobs.
   - `continuation_grace_seconds` for the shared scheduler = max across configs.
   - Existing `run_http_proxy(proxy, config, ...)` preserved as a thin delegate → `run_http_proxies([config])`;
 signature and message unchanged (tests and CLI keep working).
   - Startup: validate all configs up front, start services one-by-one; on any failure, stop the ones already started.
   - Up-front validation also rejects duplicate/overlapping listener endpoints before any socket is opened.

   ### 3.8 CLI (decision — made)

   New flags on `run` (all optional; behavior unchanged if both listen flags are absent):

   | Flag | Default | Purpose |
   |---|---|---|
   | `--comfyui-listen-port` | (off) | Extra listen port for the ComfyUI profile |
   | `--comfyui-upstream` | `http://127.0.0.1:8188` | ComfyUI-Forge server the proxy relays queued routes to |
   | `--http-settle-poll-seconds` | `1.0` | History-poll interval for settle routes |

   `run` builds one config per enabled backend (Ollama profile keeps `--http-bypass-route` additions; ComfyUI profile
 ignores those) and calls `run_http_proxies`. Validation rule kept: listener and upstream must not be the same
 host+port (per-URL check in `config.validate()`).

   ## 4. File-by-file change list

   1. **`file_proxy/http_proxy.py`** (bulk of the work)
      - `BackendProfile` dataclass + `OLLAMA_PROFILE` / `COMFYUI_PROFILE`.
      - `COMFYUI_QUEUE_ROUTES`, `COMFYUI_SETTLE_ROUTES`, `comfyui_resource_key()`.
      - `HttpProxyConfig`: + `settle_poll_seconds`, + `profile` (defaults preserve backwards compat) and validation of
 the new field.
      - `HttpProxyService`: profile-driven `_route_is_queued()`; buffered settle-response helper;
 `_run_prompt_lifecycle()` + `_wait_for_prompt_completion()` + prompt-ID preparation/validation; `handle()` transfers
 ownership before dispatch; `stop()` cancels/gathers lifecycle tasks; `_lifecycle_tasks` in `__init__`.
      - `run_http_proxies()` + backward-compatible `run_http_proxy()`.
      - Relay error text generalized from "Ollama upstream …" to "upstream …" (statuses 502/504 unchanged; tests assert
 status only).
   2. **`file_proxy/cli.py`**
      - Imports: `COMFYUI_PROFILE`, `run_http_proxies` (keep `run_http_proxy` import only if
 still referenced — switch import list).
      - New arg flags (3.8); run branch builds config list and calls `run_http_proxies`.
   3. **`tests/test_http_comfyui.py`** (new) — see §5.
   4. **`file_proxy/scheduler.py`** — *no changes* (the lifecycle task interacts only with existing
 `finish`/`cancel`/`closed`).
   5. **`file_proxy/engine.py`** — *no changes* (resource-key prefix logic already covers `comfyui:`).
   6. **`run_file_proxy.ps1` and `run_file_proxy.bat`** — optional `AI_PROXY_COMFYUI_PORT` and
 `AI_PROXY_COMFYUI_UPSTREAM` forwarding; no ComfyUI listener when the port is unset.
   7. **`README.md`** — new "Queued ComfyUI HTTP proxy" section after the Ollama section:
      - Why: GPU serialization + slot held for the full generation; client contract (queue via proxy, ws + images
 direct to `:8188`); resource key explanation; flag table (incl. `--http-settle-poll-seconds`); example launch line:
        ```powershell
        .\.venv\Scripts\file-proxy.exe --root "C:\path\to\AI_Queue" --registry-dir ".\registors" run `
          --http-listen-port 11433 --ollama-upstream "http://127.0.0.1:11434" `
          --comfyui-listen-port 18188 --comfyui-upstream "http://127.0.0.1:8188"
        ```
      - Notes: `/api` aliases, serialized destructive controls, settle-timeout behavior,
 `X-AI-Proxy-Request-ID` header, memory-only queue, and browser Host/Origin caveat. The supported integration is an
 API client such as Zet; the ComfyUI web UI and websocket continue to use the direct upstream origin.

   ## 5. Test plan (`tests/test_http_comfyui.py`)

   Helpers match `tests/test_http_proxy.py` (`unused_port()`, `make_proxy(tmp_path)`).

   1. **`test_comfyui_prompt_holds_slot_until_history_settles`** — mock upstream: `POST /prompt` returns `{"prompt_id":
 "<first>"}` immediately; `GET /history/<id>` returns `{}` until an event is set, then `{"<id>": {...}}`. Client A
 posts; assert A got the response while B's `POST /prompt` (as a task) has **not** reached upstream; assert a bypass
 route (`GET /history/<first>` through the proxy) still works while the slot is held; set the event; assert B reaches
 upstream within a timeout.
   2. **`test_comfyui_settle_timeout_releases_slot`** — `upstream_timeout_seconds=~0.35`, `settle_poll_seconds=0.05`,
 history never settles; second prompt still starts after the deadline.
   3. **`test_comfyui_resource_key_*`** (unit) — `{"prompt": workflow}` bodies for both `/prompt` and
 `/api/prompt` (including `CheckpointLoaderSimple` and nested nodes), missing `ckpt_name` → `None`, invalid JSON →
 `None`, SD-WebUI body → `None`, non-queued route (`GET /prompt`) → `None`.
   4. **`test_comfyui_bypass_routes_while_slot_held`** — `POST /interrupt` and `POST /upload/image` pass through
 immediately while a first prompt holds the slot.
   5. **`test_comfyui_validation_error_releases_immediately`** — upstream returns a non-2xx validation error; a
 second prompt begins right away.
   6. **`test_route_policy_tables`** (unit) — `service._route_is_queued` on both profiles: ComfyUI queues only the
 listed generation and destructive-control POST routes; Ollama profile queues `/api/chat` but bypasses `/api/tags`;
 an extra `HttpProxyConfig.bypass_routes` entry affects Ollama only.
   7. **`test_mixed_ollama_and_comfyui_share_queue`** — one `Proxy` + one `HttpQueue`, two `HttpProxyService`s (one per
 profile, two ports) + one `run_scheduler`; comfy prompt A holds (unsettled history); an Ollama `/api/chat` task
 through the Ollama port does not reach upstream; settle → Ollama chat proceeds; then a new comfy prompt waits behind
 it.
   8. **`test_comfyui_disconnect_after_dispatch_does_not_release_slot`** — upstream accepts the prompt, delay its
 response while the client disconnects, then return `prompt_id`; assert a second queued job cannot start until history
 settles.
   9. **`test_comfyui_api_alias_queues_and_settles`** — `POST /api/prompt` holds and polls
 `/api/history/<id>`.
   10. **`test_empty_history_object_does_not_settle`** — explicitly verify JSON `{}` does not release the slot.
   11. **`test_destructive_controls_are_serialized`** — `POST /queue` and `POST /history` do not reach upstream while
 a prompt lifecycle owns the slot; `POST /interrupt` still bypasses immediately.
   12. **`test_multi_listener_shutdown_finishes_active_job`** and duplicate-listener validation.
   13. **`test_prompt_id_is_known_before_dispatch`** — preserve a valid supplied UUID, generate one when absent,
 recalculate `Content-Length`, follow a different valid upstream response ID with a warning, and retain the slot on
 ambiguous transport failure until history/deadline.

   ## 6. Risks / edge cases considered

   - **Handler cancellation vs slot hold** — solved by lifecycle ownership before dispatch (§3.4), including the
 accepted-upstream/dropped-downstream race.
   - **Upstream errors on `/prompt`** — definite non-2xx validation errors release immediately; ambiguous transport
 failures after dispatch retain the slot and reconcile the preassigned prompt ID.
   - **Stuck generation** — bounded by settle deadline (default 2 h) with logged warning; the lifecycle task then
 releases.
   - **Client disconnect while queued** — cancels normally before ownership transfer. After upstream dispatch starts,
 the service-owned lifecycle continues regardless of the downstream connection.
   - **Queue/history mutation races** — destructive mutation routes are serialized behind the tracked prompt;
 read-only polling and `/interrupt` still bypass.
   - **Two proxies on one machine / double-queueing** — out of scope, same as today.
   - **ComfyUI version drift** — settlement requires a valid response/preassigned prompt ID and a parsed keyed history
 entry; `{}` never settles. Both native and current `/api` aliases are covered.
   - **Forge missing some sdapi routes** — listed queue routes 404 fast at upstream; client gets the error; no state is
 corrupted because these routes settle on response EOF rather than prompt history.

   ## 7. Open decisions (none blocking implementation)

   1. **`/sdapi/v1/postprocess` & `/middleware`** — currently *not* in the queue list (postprocess = lightweight
 face-fix pass we may want queued; middleware is non-standard). Easy one-line change either way; leaving out for now.
   2. **Per-backend settle deadlines** — currently reuse `upstream_timeout_seconds`; a dedicated
 `--comfyui-settle-timeout-seconds` could be added later if a 2 h default proves too long/short.
   3. **`keep_alive`-style nudges** — none needed for ComfyUI (no model-keepalive concept).

   ## 8. Verification

   1. `pytest` full suite (existing `test_http_proxy.py` must pass unmodified — they validate the backward-compat
 guarantees).
   2. New `tests/test_http_comfyui.py` green.
   3. Manual smoke: real ComfyUI-Forge on `:8188`; `file-proxy run --comfyui-listen-port 18188 ...`; from two clients,
 fire two `/prompt` posts concurrently through the proxy and confirm (a) upstream only ever runs one at a time, (b)
 second prompt blocks ~duration of first, (c) `/history` polling via proxy works while held, (d) Ctrl-C during a held
 generation exits without hanging (lifecycle-task cancellation).
   4. Cross-backend: start Ollama + ComfyUI together; confirm an in-flight `/prompt` blocks a concurrent `/api/chat`,
 and an `ollama:` job after a `comfyui:` job triggers the Forge unload (visible in proxy log).
 ```

 Key decisions already made (so nothing is silently deferred):

 - Profile-based routing to solve the inverse-queue-model problem without touching Ollama behavior.
 - Parsed history-entry settle (not body length, EOF, or /ws) for `/prompt` and `/api/prompt`, bounded by one explicit
 whole-lifecycle deadline.
 - No websocket proxying — clients go direct to :8188 for progress, and /view for images.
 - A service-owned lifecycle task owns slot release before upstream dispatch for settle routes, so handler
 cancellation cannot release an accepted prompt.
 - comfyui:<ckpt_name> resource key from workflow ckpt_name fields — zero changes to engine.py/scheduler.py.
 - One shared HttpQueue for both backends via new run_http_proxies, with run_http_proxy kept as a delegate so existing
   CLI/tests are untouched.
 - Backwards-compatible config extension (profile, settle_poll_seconds added with defaults).

 If this looks right, I'll implement in this order: http_proxy.py → cli.py → tests/test_http_comfyui.py → README,
 running the existing suite after the first two steps.
 
