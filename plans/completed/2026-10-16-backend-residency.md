# Retain idle managed ComfyUI and remove retired subscriber references

Status: completed

Owner: Codex

Started: 2026-10-16; updated: 2026-10-16

## Goal and current behavior

Keep managed ComfyUI available after work completes to avoid repeated startup
costs. [BackendManager.released](../../file_proxy/backend.py) currently starts a
60-second idle-stop task. Ollama dispatch already stops owned ComfyUI before
starting work. The retired subscriber registry is already absent; only stale
documentation and a commented environment setup line remain.

## Desired behavior and impact

Idle time never stops managed ComfyUI. Existing scheduling order, Ollama handoff,
failure reconciliation, shutdown, and manual-process ownership remain intact.
Retain the legacy `idle_shutdown_seconds` config field as an ignored compatibility
setting so existing configurations still load; omit it from the example.

Affected paths: backend/runtime configuration, example config, existing lifecycle
scenario assertions, environment setup, README and relevant Docs. No new tests,
dependencies, external writes, real queue changes, or subscriber code changes.
Preserve [process/scheduling contracts](../../Docs/INVARIANTS.md).

## Luna-sized work packets

1. **Remove stale subscriber references**
   - Paths: `Set_Env.ps1`, `Docs/DATA_MODEL.md`, `Docs/DEVELOPMENT.md`, `Docs/CODE_MAP.md`.
   - Depends on: inspected registry directory and repository-wide reference search.
   - Changes: remove obsolete environment line and docs/invalid registry link.
   - Preserve: Zet registration and runtime/queue data.
   - Checkpoint: only Zet is registered; no retired subscriber references in active files.
   - Validation: repository search and read-only registry loading.
2. **Retain managed ComfyUI while idle**
   - Paths: `file_proxy/backend.py`, `file_proxy/runtime.py`, `runtime-config.example.json`.
   - Depends on: inspected backend preparation/release, scheduler and shutdown paths.
   - Changes: remove idle task creation/cancellation and timer implementation;
     preserve ready state for a live owned process; document ignored legacy field.
   - Preserve: non-preemption, explicit Ollama handoff, shutdown and recovery stops.
   - Checkpoint: no idle termination path; existing configs remain accepted.
   - Validation: existing isolated ComfyUI lifecycle scenario.
3. **Synchronize lifecycle assertions and documentation**
   - Paths: existing lifecycle scenarios, README, affected Docs, residency ADR.
   - Depends on: packet 2.
   - Changes: update assertions/synchronization inside existing approved lifecycle
     scenarios for idle retention; document tradeoff and compatibility. No new
     scenarios, branches or coverage matrix.
   - Preserve: five isolated scenarios plus one gated live scenario.
   - Checkpoint: existing suite passes; docs reflect idle retention and handoff.
   - Validation: venv pytest, CLI help, scoped Markdown link check, git diff --check
     and final diff review. Do not run real queue or opt-in live backend scenario.

## Risks and alternatives

ComfyUI keeps memory occupied while idle; this is explicitly requested. The next
scheduled Ollama job still frees it. No proactive preload/restart after Ollama:
that would require a broader scheduling policy. ComfyUI's internal model cache
and clients bypassing the proxy are outside this lifecycle policy.

## Progress and results

All three packets completed. No initial user changes detected
(`git status --short` empty). The private runtime config omits the idle setting,
so it needs no edits. Kept legacy timeout compatibility instead of requiring
machine-local config migration. No new test scenarios or branches added.

## Final results and observed validation

- Changed `file_proxy/backend.py` to retain the owned process while idle;
  `file_proxy/runtime.py` labels the ignored compatibility field and
  `runtime-config.example.json` omits it.
- Removed the obsolete environment setup line from `Set_Env.ps1` and corrected
  subscriber references in data/development/code-map documentation. A repository
  reference search found no remaining retired subscriber references. Read-only
  registry loading with its old environment variable absent reports only `zet`.
- Updated existing `tests/test_e2e.py` and `tests/test_live_backends.py` lifecycle
  assertions/synchronization, README, architecture, managed runtime, data model,
  code map, development guide and index. Added
  [ADR 0003](../../Docs/DECISIONS/0003-retain-idle-comfyui.md).
- `.\.venv\Scripts\python.exe -m pytest tests/test_e2e.py::test_comfyui_settlement_handoff_and_cached_result_survive_restart -q`:
  passed, 1 test in 4.32 seconds.
- `.\.venv\Scripts\python.exe -m pytest -q`: passed, 5 tests and 1 live scenario
  deselected in 9.31 seconds. The existing lifecycle scenario verifies that the
  same process remains ready beyond a short legacy idle interval and still stops
  for scheduled Ollama work.
- `.\.venv\Scripts\python.exe -m file_proxy.cli --help`: passed.
- Example and existing private runtime JSON load successfully.
- Local Markdown link check: 114 links in changed/new docs resolved before
  archiving; rechecked after archiving. `git diff --check` passed; final source,
  config, scenario and documentation diffs reviewed for unintended changes.
- Live GPU/backend validation was deliberately not run; no real queue or running
  proxy was changed. Restart the AI_Proxy process to apply the new lifecycle and
  reload the registry. Internal ComfyUI model residency remains backend-owned.
