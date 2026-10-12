# ADR 0002: Shared slot with explicit backend and prompt ownership

- Status: retrospective record of current implementation, reconstructed 2026-10-11.
- Evidence: [engine.py](../../file_proxy/engine.py), [scheduler.py](../../file_proxy/scheduler.py),
  [http_proxy.py](../../file_proxy/http_proxy.py), [backend.py](../../file_proxy/backend.py),
  [ComfyUI lifecycle scenario](../../tests/test_e2e.py).

## Context

Local model/image work competes for limited resources. A ComfyUI submission response
acknowledges asynchronous work; it does not prove generation finished. Manual
ComfyUI instances can also occupy the managed endpoint.

## Decision

Serialize expensive HTTP and file work through one non-preemptive scheduler with
bounded affinity/backend ordering. ComfyUI prompt lifecycle tasks own the slot
through keyed history settlement/reconciliation after response delivery. Managed
mode owns only processes it starts, unloads Ollama before starting ComfyUI, stops
owned ComfyUI before Ollama, and refuses manual-instance adoption. External
unresolved generation retains the slot; accepted HTTP work is never replayed.

## Rationale and tradeoffs

README states the limited-local-resource purpose. Code/tests establish why early
response completion is insufficient to release a slot and why ownership checks
protect manual processes. Historical reasons for exact streak/grace/deadline
values, SQLite cache choice and ordering algorithm evolution are unknown.

Alternatives and their original consideration are unknown. Independent per-backend
slots or release-on-submission would allow overlap under current shared-resource
assumptions. Current design can block later work on external ambiguity, sacrifices
some FIFO ordering for affinity, and can defer file work under same-backend HTTP
load. It does not coordinate clients talking directly to raw backends.

## Consequences and related knowledge

New execution paths must define start/finish ownership, cancellation and backend
identity. Do not replace prompt settlement with response closure or terminate an
unowned backend. Managed-output cleanup additionally depends on successful cache
publication and containment checks, not on arbitrary upstream success.

See [SCHEDULING.md](../COMPONENTS/SCHEDULING.md),
[HTTP_TRANSPORT.md](../COMPONENTS/HTTP_TRANSPORT.md),
[MANAGED_RUNTIME.md](../COMPONENTS/MANAGED_RUNTIME.md) and
[INVARIANTS.md](../INVARIANTS.md).
