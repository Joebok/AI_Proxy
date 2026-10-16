# ADR 0003: Retain managed ComfyUI while idle

- Status: accepted, 2026-10-16.
- Evidence: [backend.py](../../file_proxy/backend.py),
  [runtime.py](../../file_proxy/runtime.py),
  [existing lifecycle scenario](../../tests/test_e2e.py).

## Context

The previous 60-second idle shutdown discarded the managed ComfyUI process and
its resident models even when no competing work needed the memory. Joe requested
keeping ComfyUI available to reduce image load latency.

## Decision

Retain an already running owned ComfyUI process indefinitely while idle. Release
of the execution slot clears active job identity without scheduling a stop.
Keep `idle_shutdown_seconds` accepted as an ignored legacy setting so existing
machine-local configurations continue loading. Omit it from new example config.

Preserve scheduled Ollama handoff, explicit proxy shutdown and failure-recovery
termination. Preserve queue order, non-preemption and manual-process ownership.
ComfyUI still starts on demand; do not proactively launch it after Ollama work.

## Consequences

Idle ComfyUI retains memory until a supported lifecycle transition needs to free
it. This deliberately favors image readiness over freeing unused memory on a
timer. It does not pin individual models against ComfyUI's own memory management
or coordinate clients bypassing the proxy. Lengthening the timer would still
introduce avoidable cold starts during an otherwise empty queue.

See [managed runtime](../COMPONENTS/MANAGED_RUNTIME.md) and
[execution ownership](0002-shared-execution-and-backend-ownership.md).
