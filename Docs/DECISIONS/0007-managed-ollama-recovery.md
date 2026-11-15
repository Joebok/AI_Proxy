# ADR 0007: Managed Ollama restart after inference timeout

- Status: implemented with operational cutover pending; recorded 2026-11-15.
- Evidence: `BackendManager`, `Proxy.run_scheduler`, `ai_worker.run_ollama`,
  `HttpProxyService._relay`, and isolated E2E scenarios.

## Decision

Managed Ollama mode is opt-in and starts the configured Windows Ollama executable
as an AI_Proxy-owned process. It refuses an already reachable endpoint. Each
inference is bounded by a configurable wall-clock deadline. Native file inference
may restart Ollama and retry once inside its existing execution. Queued HTTP
inference may retry once only before downstream response headers are sent. A
stream that has started is terminated and the backend is restarted before the
shared scheduler advances.

## Consequences

The managed server inherits the launching user's Ollama environment, including
the model directory and runtime settings. The tray app must be stopped before
enabling managed mode. Stopping AI_Proxy stops the owned server. The actual tray
app cutover and real timeout recovery remain unverified.

See [managed runtime](../COMPONENTS/MANAGED_RUNTIME.md),
[HTTP transport](../COMPONENTS/HTTP_TRANSPORT.md), and
[invariants](../INVARIANTS.md).
