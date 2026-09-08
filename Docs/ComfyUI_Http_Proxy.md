# Managed ComfyUI HTTP contract

AI Proxy exposes separate Ollama and ComfyUI listeners backed by one execution
arbiter. HTTP work has strict priority over filesystem work, active work is
never preempted, and a two-second continuation grace is retained. Model
affinity is bounded to five consecutive jobs; a request waiting sixty seconds
wins the next eligible selection. Queue/history mutations are ordering
barriers.

## Modes

External mode is the default. AI Proxy forwards to an already-running ComfyUI
and has no authority to stop it. If a generation cannot be reconciled after
cancellation, the GPU slot remains blocked for operator intervention.

Managed mode is opt-in through `--runtime-config`. AI Proxy starts the configured
Python executable and `main.py` directly, hidden and with `shell=False`; it does
not launch or modify Comfy Desktop. The upstream must be loopback. On Windows,
the process is assigned to a kill-on-close Job Object so descendants terminate
with the proxy. A live server at the managed address that is not owned by this
proxy is treated as a manual-instance conflict and is never terminated.

Before managed ComfyUI starts, resident Ollama models are unloaded and `/api/ps`
is checked. Before Ollama work, owned ComfyUI is stopped and its exit confirmed.
Consecutive image operations reuse ComfyUI; it stops after the configured idle
interval. Startup has one attempt per request and a cooldown after failure.

## Client routes

Submit native workflows to `POST /prompt` (or `/api/prompt`) on the queued
ComfyUI listener. AI Proxy injects a canonical UUID when none is supplied,
rejects invalid or reused IDs, and returns `X-AI-Proxy-Request-ID`. It tracks
accepted work independently of the client connection. WebSocket progress stays
on raw ComfyUI `/ws`.

For proxy-submitted prompts, retrieve `/history/{prompt_id}` and `/view` through
AI Proxy. Standard output artifacts are streamed into the local cache before
history becomes visible there, so both routes continue working after managed
ComfyUI stops or AI Proxy restarts. `/api` aliases are supported. Original
ComfyUI output files are never deleted.

`GET /_proxy/status` is available on both listeners and reports queue age,
affinity, backend ownership, cache use, and filesystem degradation.
`GET /_proxy/jobs/{request_id}` returns a durable lifecycle outcome.

Discovery and upload calls are scheduler-controlled in managed mode because
they may start ComfyUI. Status, interruption, and cached result retrieval do not
consume inference admission capacity.

## Failure and overload behavior

The per-request body limit is 100 MiB. Defaults also admit at most 32 inference
requests and 256 MiB of aggregate buffered body data, including chunked or
compressed input. Overload and backend conflicts return 503 with `Retry-After`;
individual body overflow returns 413; a 30-minute queue wait returns 504.

Generation settlement uses one monotonic 7,500-second deadline and bounded
history polls. At expiry AI Proxy requests interruption, waits 30 seconds, then
terminates only a proxy-owned backend. Accepted jobs are never replayed.

Metadata and cached artifacts live in the local runtime directory, never the
synchronized filesystem queue. Completed data is retained for seven days with
a 10 GiB limit and is evicted oldest-first as a record/artifact unit.

Shutdown stops admission, wakes undispatched clients, and cancels owned
lifecycle tasks. Durable records remain queryable after restart; no pending HTTP
body is persisted or replayed. Filesystem queue errors degrade only that worker
path and do not close API listeners.

## Configuration and coexistence

[`runtime-config.example.json`](../runtime-config.example.json) records the
inspected standalone ComfyUI Python, source tree, shared input/output folders,
and Desktop-generated model-path file. It ships with `enabled: false`; copy it
to a local private path and enable it only after machine acceptance. Configure
Zet to submit and retrieve through the proxy listener. Do not point Comfy
Desktop itself at that listener or start Desktop while managed mode owns port
8188.

Rollback is simply disabling managed mode and starting ComfyUI manually.
Installed models and original output files are unchanged.
