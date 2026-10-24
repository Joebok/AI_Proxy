# Managed backends and dashboard lifecycle

## Responsibility and interfaces

[backend.py](../../file_proxy/backend.py) `BackendManager` owns optional ComfyUI
processes and safe residency transitions; it does not own the Ollama server.
[supervisor.py](../../file_proxy/supervisor.py) `RuntimeSupervisor` owns a restartable
runtime for [tui.py](../../file_proxy/tui.py) `ProxyDashboard`. Consumers are CLI,
shared scheduler, HTTP status/lifecycle and dashboard. Inputs are validated runtime
config and backend identity; outputs are ready/blocked process state, health
snapshots, bounded logs and lifecycle transitions.

Configuration definitions/defaults belong to [DATA_MODEL.md](../DATA_MODEL.md).
Managed ComfyUI is disabled by default; external mode forwards without owning a
process. Standard file renders can use managed mode without a ComfyUI HTTP listener.

## Backend transition workflow

`prepare_for` refuses work if blocked, prepares the selected
backend, then records active backend/job. ComfyUI work calls `ensure_comfyui`:

1. Under async lock, probe `/queue`; a live owned healthy process is reused.
2. An unowned reachable endpoint (including port-open fallback) is a manual
   conflict: set external ownership/blocked reason, never adopt/terminate it.
3. Refuse startup during cooldown. Query Ollama `/api/ps`, POST each resident
   model to `/api/generate` with `keep_alive: 0`, and verify no models remain.
4. Start configured Python/script/argv directly with `shell=False`; optionally
   append `--extra-model-paths-config`. Readiness requires `/queue` JSON lists
   `queue_running` and `queue_pending`. CUDA stats are not queried for readiness.
5. On exit/readiness failure, terminate owned process and set cooldown. The
   startup deadline is anchored to the pre-unload timestamp, so unload can use
   some startup budget; it is not an additional unconditional full deadline.

Managed Ollama work first calls `stop_comfyui`, terminates only owned process and
confirms endpoint no longer reachable. Shutdown uses terminate/wait/deadline/kill.
Windows starts hidden and suspended, assigns a kill-on-close Job Object for
descendants, then resumes the process. Non-Windows termination addresses the
process; equivalent descendant containment is not implemented/verified.

`released` clears active identity and leaves a live owned ComfyUI process ready
for reuse indefinitely while idle. There is no idle-stop task; legacy
`idle_shutdown_seconds` values are accepted but ignored. Only scheduled Ollama
handoff, shutdown (`close`), or failure recovery stops the owned backend. This
retains memory to reduce subsequent image startup latency without changing queue
ordering or preempting active work. ComfyUI starts on demand; completing Ollama
work does not proactively restart it. Backend logs rotate by size into local
runtime and maintain recent log and transition deques. Health probes run
concurrently: Ollama `/api/ps` for reachability
and model residency, ComfyUI `/queue` plus port fallback for ownership conflict.
Ready listener, reachable endpoint, healthy endpoint, and owned process are
different observations; do not equate them.

## Forge handoff is separate

`Proxy._prepare_resource` in [engine.py](../../file_proxy/engine.py) best-effort
POSTs Forge `/sdapi/v1/unload-checkpoint` when entering an `ollama:` key from startup
or non-Ollama affinity. Default CLI upstream is loopback 7860; constructor default
is no Forge URL. Failures normally do not fail/log an error for the job;
`--require-forge-cleanup` turns supported failure paths into blocked HTTP start or
FAILED file `RESOURCE_TRANSITION`. This handoff is not managed Forge process
ownership, and it keys off resource string rather than registry backend alone.

## Supervisor and dashboard

`RuntimeSupervisor.start/stop/restart` serialize transitions under an async lock.
If listeners or managed mode exist, start calls `run_http_proxies` with
`HttpRuntimeState` and waits for its readiness event or startup task failure.
Filesystem-only external mode runs a thread with a stop event, local lock and
interrupted recovery; it stops between jobs rather than killing workers.

`snapshot` gathers file status, HTTP queue/admission, backend process and endpoint
health, store/cache state and logs. State becomes busy when work is active, degraded
for filesystem/backend/cache error, or error on runtime task/startup failure.
Some display values are observations, not authoritative job lifecycle states.
Stop closes listeners/admission, then signals thread stop or cancels async runtime
and awaits cleanup. Restart uses existing parsed config/registry objects; it does
not reload changed configuration files from disk. Restart the CLI for new settings.

Dashboard mounts with automatic start, refreshes once per second, renders snapshots,
and invokes supervisor via key actions. Logs use a 2,000-entry bound and sequence
cursor; markup from messages is escaped. File execution start, finish and collision
entries use the trusted backend; HTTP listener entries use their profile, shown as
`ComfyUI (file)`, `Ollama (http)`, etc. File workers without a backend declaration
show their worker name. General queue messages show `Proxy (file)` or
`Proxy (http)`. `S`: stop and keep UI; `R`: restart;
PgUp/PgDn: scroll; End: follow; Q/Ctrl+C: safe stop and exit.

## Failure modes, extension, verification

Missing local paths/model config, unmanaged port occupant, failed Ollama unload,
unhealthy owned process, cooldown, endpoint remaining active after stop and
listener bind conflict are distinct failures. A recorded `blocked_reason` has no
automatic operator-clear method; reconcile the external condition and deliberately
restart a fresh runtime as appropriate. Do not add automatic process adoption.

Native file-worker ambiguity has a separate durable `unsettled_comfyui.json`
admission guard. The managed scheduler recovers this automatically by calling
`stop_comfyui`, which stops only owned processes and verifies the endpoint is
gone. It then archives the guard and continues queued work. A stopped backend
also satisfies this check after restart. Stop/probe/archive failures keep the
guard; a reachable unowned process still requires intervention. Failed/interrupted
attempts retain their terminal outcomes and are never replayed. This recovery
does not clear arbitrary `blocked_reason` values or change HTTP settlement.
Guard archival retries sharing violations for up to 15 seconds off the asyncio
event loop; if the retry expires, the existing scheduler retry keeps admission held.

Backend additions need registration/profile validation, lifecycle/health contracts,
scheduler backend resolution, CLI/TUI configuration and tests; a new name alone is
insufficient. Dashboard additions should extend `RuntimeSnapshot`, keeping work in
supervisor/manager rather than blocking rendering.

[ComfyUI lifecycle scenario](../../tests/test_e2e.py) verifies disposable managed
process startup/stop, keyed prompt settlement, Ollama scheduling handoff and
cached history/artifact survival after restart. The dashboard scenario verifies
interrupted recovery and worker-safe restart. [Opt-in live lifecycle scenario](../../tests/test_live_backends.py)
checks installed Ollama residency and managed ComfyUI model discovery, handoff and
idle retention. Real generation, Dropbox and non-Windows process containment remain
unverified.

Related: [contracts](../INVARIANTS.md),
[execution ownership ADR](../DECISIONS/0002-shared-execution-and-backend-ownership.md).
Idle residency policy: [ADR 0003](../DECISIONS/0003-retain-idle-comfyui.md).
