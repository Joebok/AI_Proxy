# Restart Ollama and retry timed-out inference

Status: active

Owner: Codex

Started: 2026-11-15; updated: 2026-11-15

## Goal

Manage the Windows Ollama server under AI_Proxy ownership and enforce a five-minute inference deadline. Restart after timeout and retry one time within native file jobs and eligible queued HTTP requests.

## Current Behavior

`BackendManager` owns managed ComfyUI but treats Ollama as external. Native `ai_worker.run_ollama` allows 7,500 seconds and `worker_host` owns a longer worker deadline. HTTP Ollama routes share `generation_settlement_seconds`, default 7,500 seconds. The two confirmed Style/Write Prompt attempts used bare `gemma4:12b`, no request token cap, and generated tens of thousands of tokens. Zet already retries interrupted attempts, but one recovery attempt received connection refused.

## Desired Behavior

Managed Ollama starts and is contained by AI_Proxy. Each inference attempt has a configurable 300-second wall deadline. The first timeout restarts the owned server and retries exactly once inside the same native job or pre-header HTTP response. A second timeout returns a terminal failure. Recovery failure holds local scheduling and exposes status.

## Impact Analysis

| Files/modules | Interfaces/data/consumers affected | Tests and documentation |
| --- | --- | --- |
| runtime/backend/scheduler/CLI | New optional managed Ollama settings and process lifecycle; existing ComfyUI ownership stays intact | Existing scheduler, dashboard and live lifecycle scenarios; managed runtime docs |
| ai_worker/worker_host/engine | Native inference attempts, local execution witness and structured terminal result | Existing isolated file scenario only; AI provider and invariants docs |
| http_proxy | Queued Ollama request deadline/retry and stream cancellation | Existing streaming scheduler scenario; HTTP transport docs |
| plans and docs | Windows cutover and rollback contract | Development, component and invariant docs; ADR |

## Invariants and Constraints

Preserve single execution ownership, local slot non-preemption, immutable envelopes, bounded process shutdown, shell-free trusted launch, cloud lane independence, no replay of ambiguous completed work, and one proxy per queue. Managed mode is opt-in. Do not change the user's tray app, startup registration, runtime config, or real queue during implementation.

## Implementation Steps

1. **Packet 1: Managed server lifecycle and configuration**
   - Owned paths/symbols: `file_proxy/runtime.py`, `file_proxy/backend.py`, `file_proxy/cli.py`, `runtime-config.example.json`.
   - Depends on: none.
   - Changes: Add optional Ollama executable/bind/deadline settings; process ownership, Job Object containment, endpoint readiness, bounded stop and conflict handling; support run/TUI and reject once.
   - Preserve: Existing ComfyUI manager behavior and user-owned process safety.
   - Checkpoint: Disabled defaults are compatible; configured server can start/probe/stop.
   - Validation: CLI help and existing ComfyUI lifecycle scenario.

2. **Packet 2: Native file execution restart/retry**
   - Owned paths/symbols: `file_proxy/worker_host.py`, `file_proxy/ai_worker.py`, `file_proxy/engine.py`, lifecycle coordination helpers.
   - Depends on: Packet 1.
   - Changes: Coordinate timeout/restart with execution identity and bounded acknowledgments; enforce deadline; retry native request once; classify final timeout/recovery failure. Recover and retry legacy worker watchdog timeout once under managed ownership.
   - Preserve: Running envelope and worker witness while retrying; no Zet-level automatic retries for a final timeout.
   - Checkpoint: One answer after one retry; final failure after two sends; next job is admitted after recovery.
   - Validation: Existing file/dashboard scenarios and disposable lifecycle check.

3. **Packet 3: Queued HTTP deadline and recovery**
   - Owned paths/symbols: `file_proxy/http_proxy.py`, scheduler/backend coordination.
   - Depends on: Packets 1 and 2.
   - Changes: Apply per-attempt deadline, retry once before headers, abort and recover after a streamed timeout, and hold local admission through recovery.
   - Preserve: Queue wait semantics, request IDs, stream diagnostics, non-preemption.
   - Checkpoint: Pre-header retry returns final result; post-header timeout cancels stream and recovers backend before subsequent work.
   - Validation: Existing streaming scheduler scenario and disposable HTTP checks.

4. **Packet 4: Operational and contract docs**
   - Owned paths/symbols: Relevant `Docs/COMPONENTS`, `Docs/INVARIANTS.md`, `Docs/DEVELOPMENT.md`, index, ADR.
   - Depends on: Packets 1–3.
   - Changes: Describe ownership, limits, retry outcome, tray-app cutover and rollback. Keep real cutover as an operator action.
   - Preserve: Docs/ index ownership and no claims beyond observed validation.
   - Checkpoint: Links resolve and config example matches the implemented interface.
   - Validation: Link inspection and `git diff --check`.

## Validation

Run existing targeted E2E cases for file execution, shared streaming scheduler, dashboard recovery and ComfyUI handoff with `.venv`. Inspect CLI help and docs links and run `git diff --check`. Use disposable process/queue checks for timeout/retry, startup down, occupied port, restart failure and post-header timeout. Do not exercise the actual shared queue or switch tray ownership.

## Risks and Alternatives

HTTP streaming cannot transparently retry after response headers have been sent; terminate that stream, recover Ollama, and let the caller observe the ambiguous timeout. Native jobs can retry before publication within one envelope. Managed ownership requires a deliberate Windows tray-app cutover that is outside this code change.

## Progress and Decisions

| Packet | Status | Discoveries, deviations, remaining work |
| --- | --- | --- |
| 1 | complete | Managed config, shell-free process launch, Windows Job Object containment, readiness probe and run/TUI wiring are implemented. Installed Ollama lifecycle smoke passed. |
| 2 | implemented, unverified | Native worker signals the scheduler through the execution journal and retries once after restart. Failure cooldown holds local work. No controlled inference timeout was induced. |
| 3 | implemented, unverified | HTTP applies the configured deadline, retries before headers, and aborts/restarts after streamed timeout. Timeout paths lack controlled verification. |
| 4 | partial | Docs and ADR updated. Live cutover was made. Tray startup shortcut is disabled and local config enables managed mode. Timeout/retry acceptance remains. |

## Final Results

Pending implementation and validation.
