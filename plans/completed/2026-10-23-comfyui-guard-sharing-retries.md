# ComfyUI guard sharing retries

Status: completed

Owner: Codex

Started: 2026-10-23; updated: 2026-10-23

## Goal

Prevent transient Windows sharing violations from failing native ComfyUI workers
while updating their local submission guard or producing a recoverable archive.
Preserve the durable ambiguity fence, backend ownership, and no-replay behavior.
No public API, payload, or configuration changes are in scope.

## Current Behavior

`file_proxy.ai_worker.write_json` atomically replaces JSON without retry. The
observed `ai_result.json` records `PermissionError`/`WinError 5` replacing
`unsettled_comfyui.json`; the saved submission remained `PREPARED` while the
local guard evidence showed `DISPATCHING`. `Proxy._reconcile_unsettled_comfyui`
stops and verifies its managed ComfyUI process, then renames the guard without
retry. A transient `WinError 32` is caught by the scheduler and recovery succeeds
on a later poll. Existing engine atomic replacements already retry
`PermissionError` for 15 seconds with exponential backoff.

Relevant contracts: [native ambiguity and recovery](../../Docs/COMPONENTS/AI_PROVIDERS.md),
[managed runtime](../../Docs/COMPONENTS/MANAGED_RUNTIME.md), and
[invariants](../../Docs/INVARIANTS.md). The approved test scope is one isolated
regression scenario; no live queue/backend calls.

## Desired Behavior

Worker atomic JSON replacements and guard deletion retry transient permission
failures for up to 15 seconds, starting at 50 ms and capping delay at 1 second.
The `DISPATCHING` guard remains durable before `/prompt`; retries never repeat
the POST. Exhausted retries fail the worker and preserve the last durable
ambiguity evidence. Managed recovery applies the same retry policy to guard
archival off the asyncio event loop, and only after verified backend stop. A
persistent archive failure continues to hold local admission.

## Impact Analysis

| Files/modules | Interfaces/data/consumers affected | Tests and documentation |
| --- | --- | --- |
| `file_proxy/ai_worker.py` | Internal native worker JSON persistence; no payload/schema change | One controlled worker + fake ComfyUI regression scenario |
| `file_proxy/engine.py` | Internal managed guard archival; admission and backend ownership contracts unchanged | Existing suite and focused regression |
| `tests/test_e2e.py`, provider/runtime/development docs | Maintained scenario count increases by one with Joe's approval; document retry and fail-closed behavior | Focused scenario, full isolated suite, diff and link checks |

## Invariants and Constraints

- Write and flush `DISPATCHING` before posting a prompt; never replay an ambiguous
  submission or rewrite a terminal envelope.
- Only archive an ambiguous guard after stopping and verifying the proxy-owned
  backend. External ownership and persistent I/O errors remain held.
- Do not modify existing user changes in engine/supervisor/TUI/runtime docs except
  to add the focused recovery documentation.
- Use the project virtual environment and avoid the real queue/live backend.

## Implementation Steps

1. **Packet 1: Retry worker persistence.**
   - Owned paths/symbols: `file_proxy/ai_worker.py` `write_json` and settled guard
     deletion.
   - Depends on: none.
   - Changes: Add bounded `PermissionError` retries matching engine backoff for
     atomic JSON replacement and guard unlink. Retain existing write ordering and
     allow exhausted errors to propagate through normal worker failure reporting.
   - Preserve: durable ambiguity evidence; one `/prompt` call at most.
   - Checkpoint: a single injected replace failure is retried and then succeeds.
   - Validation: focused worker scenario in Packet 3.

2. **Packet 2: Retry managed guard archival.**
   - Owned paths/symbols: `file_proxy/engine.py`
     `Proxy._reconcile_unsettled_comfyui`.
   - Depends on: none.
   - Changes: Call existing `_replace_with_retry` through `asyncio.to_thread` after
     verified backend stop; let exhausted failure reach the scheduler's existing
     held/retry path.
   - Preserve: stop-before-archive ordering and fail-closed admission.
   - Checkpoint: transient archive lock no longer emits a failed recovery attempt;
     persistent failure leaves admission blocked.
   - Validation: existing isolated suite and focused source review.

3. **Packet 3: Verify and document.**
   - Owned paths/symbols: `tests/test_e2e.py`; provider and managed-runtime docs.
   - Depends on: Packets 1 and 2.
   - Changes: Add one isolated scenario using a controlled fake ComfyUI endpoint;
     inject a one-time `PermissionError` on the `ACCEPTED` guard replacement.
     Assert successful worker result, exactly one prompt POST, and guard removal
     after settlement. Document retry timing and persistent-failure behavior.
   - Preserve: no real queue/backend use; keep existing user documentation edits.
   - Checkpoint: scenario and all required validation pass.
   - Validation: focused scenario; full `pytest -q`; `git diff --check`; validate
     introduced documentation links; inspect final diff.

## Validation

Use `.\.venv\Scripts\python.exe -m pytest tests/test_e2e.py::<new-test-name> -q`,
then `.\.venv\Scripts\python.exe -m pytest -q`, `git diff --check`, and a targeted
link validation for changed Markdown. Baseline before changes: 6 passed, 1 live
scenario deselected. Do not invoke runtime commands against the real queue.

## Risks and Alternatives

A persistent ACL denial or long-lived file handle still fails after 15 seconds;
this is intentional so the worker reports the failure and the scheduler remains
fenced. The process holding the guard is not known. Retrying all native worker
atomic JSON replacements is a small internal scope extension that also helps
other worker manifests on transiently locked storage.

## Progress and Decisions

| Packet | Status | Discoveries, deviations, remaining work |
| --- | --- | --- |
| Plan record | complete | User approved the plan and one focused regression scenario. Existing uncommitted changes were inspected and preserved. |
| Packet 1 | complete | Worker JSON replace and settled guard unlink use 15-second permission retries. |
| Packet 2 | complete | Managed guard archive calls the existing retry helper in a worker thread after backend stop. |
| Packet 3 | complete | Regression scenario and provider/runtime/development docs added; focused and full isolated checks passed. |

## Final Results

Implemented worker and managed recovery retries, added the approved regression
scenario, and updated provider, managed-runtime, and development guidance.

Validation observed:

- Focused scenario: **1 passed**.
- `.\.venv\Scripts\python.exe -m pytest -q`: **7 passed, 1 live scenario deselected**.
- `git diff --check`: passed; Git reported existing LF-to-CRLF normalization warnings.
- Changed Markdown links: validated; all targets exist.
- No real queue or live backend was used.

The original sharing-handle owner remains unknown. Persistent permission failures
still fail after the bounded retry and keep ambiguous local execution fenced.
