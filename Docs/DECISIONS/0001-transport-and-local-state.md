# ADR 0001: Keep application state and worker code outside transport

- Status: retrospective record of the current boundary, reconstructed 2026-10-11.
- Evidence: [README](../../README.md), [engine.py](../../file_proxy/engine.py),
  [registry.py](../../file_proxy/registry.py), [store.py](../../file_proxy/store.py).

## Context

Queue envelopes can arrive partially through an eventually consistent sync
service. Subscribers have different task schemas and local installations. Executing
sync-delivered worker code or storing application truth in listings would couple
business behavior to partial replicas.

## Decision

Transport owns immutable asks/answers, safe references, scheduling and a single
worker attempt. Registry supplies trusted local command argv. Subscribers own
publication, prompts/payload semantics, retries and durable/idempotent harvest.
Local runtime owns lock, execution outcomes, HTTP metadata/cache/diagnostics;
undispatched HTTP bodies are memory-only and never replayed.

## Rationale and tradeoffs

README explicitly explains partial-code races, queue-writer control over executable
code, and eventual-sync ambiguity. The reason for the exact timing of the move to
local runtime locking/journals is unknown; no historical rationale is inferred.
SQLite versus alternative persistence and local-lock design history are unknown.

This boundary allows unrelated apps to share transport but places idempotency and
retention work in each subscriber. A local lock does not ensure distributed
ownership; one assigned proxy per queue is an operational constraint. Single
attempt avoids automatic duplicate effects but cannot remove crash ambiguity.

## Consequences and related knowledge

Do not import subscriber applications, accept queue commands, add proxy-wide system
prompts or infer subscriber business state from queue paths. The narrow effort
normalization extension is described in [REASONING_POLICY.md](../COMPONENTS/REASONING_POLICY.md).
Protocol compatibility and local schema changes need separate impact analysis.
See [FILE_TRANSPORT.md](../COMPONENTS/FILE_TRANSPORT.md),
[DATA_MODEL.md](../DATA_MODEL.md), [INVARIANTS.md](../INVARIANTS.md).
