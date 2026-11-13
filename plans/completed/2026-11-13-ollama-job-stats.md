# Ollama job statistics

Status: completed

Owner: Codex

Started: 2026-11-13; updated: 2026-11-13

## Goal

Capture prompt-free performance and residency metadata for native and queued Ollama work and export it as CSV.

## Current Behavior

Native Ollama worker results retained a small subset of Ollama metrics. Queued HTTP persisted lifecycle state and transport diagnostics, but had no tabular performance export. Ollama `/api/ps` was already used for runtime health and exposes model residency sizes.

## Desired Behavior

A local 90-day statistics database and read-only CSV export; instrument both execution paths without coupling statistics failures to job outcomes.

## Impact Analysis

| Files/modules | Interfaces/data/consumers affected | Tests and documentation |
| --- | --- | --- |
| ollama_stats_store, ollama_metrics, ai_worker | local performance records | existing worker scenarios; AI provider docs |
| http_proxy, ollama_stats | streamed HTTP records and `python -m file_proxy.ollama_stats` | shared scheduler and policy scenarios |
| DATA_MODEL | runtime storage description | links checked by inspection |

## Invariants and Constraints

No prompts, responses, headers, or raw errors in performance records. Preserve streaming bytes, scheduling ownership, and immutable job outcomes. No added tests per repository instructions.

## Implementation Steps

1. Store and metrics extraction. Completed. Shared SQLite rows, 90-day pruning, sampling helper.
2. Native worker and HTTP instrumentation. Completed. Worker record writing and bounded HTTP body sample with timing metadata.
3. CSV export and documentation. Completed. Read-only CSV reader and data model/provider documentation.

## Validation

`py_compile` passed. Targeted shared scheduler and reasoning-policy scenarios passed, 2 passed. Full isolated suite passed, 7 passed and 1 live scenario deselected. `git diff --check` passed. A disposable populated SQLite row exported successfully to CSV with expected identity, failure outcome, blank missing values, and residency status.

## Risks and Alternatives

Dispatched native and HTTP failures now write rows with available timing and outcome data, including when Ollama returns no response. Residency sampling uses a bounded single probe and is best-effort. Real Ollama behavior has not been exercised. Statistics writes and residency sampling run in a worker thread so SQLite contention or probe latency does not stall the HTTP event loop. HTTP write failures appear in live dashboard state and a runtime marker; native worker failures use the same marker read by the dashboard. Worker marker writes are best-effort background writes.

## Progress and Decisions

| Packet | Status | Discoveries, deviations, remaining work |
| --- | --- | --- |
| 1 | complete | SQLite schema and 90-day pruning implemented. |
| 2 | complete | Ollama metrics and `/api/ps` residency extraction implemented. |
| 3 | complete | Statistics are written after answer/schema validation, preserving returned metrics for validation failures. Dispatched calls that fail before response also get an outcome row. |
| 4 | complete | Bounded JSON/NDJSON/SSE parsing extracts terminal metrics and compatible usage. Original bytes continue to be forwarded. Transport errors write partial outcome rows. |
| 5 | complete | CSV exporter opens database read-only. |

## Final Results

Implemented files: `file_proxy/ollama_stats_store.py`, `file_proxy/ollama_metrics.py`, `file_proxy/ollama_stats.py`, `file_proxy/ai_worker.py`, `file_proxy/http_proxy.py`, `file_proxy/supervisor.py`, `file_proxy/tui.py`, `Docs/DATA_MODEL.md`, `Docs/COMPONENTS/AI_PROVIDERS.md`. Targeted tests passed (2), full isolated suite passed (7 passed, 1 deselected), compilation and `git diff --check` passed. Header-only and populated CSV exports were verified with a disposable runtime. Real Ollama residency behavior remains unverified. Dashboard shows statistics logging failures without changing execution outcome.
