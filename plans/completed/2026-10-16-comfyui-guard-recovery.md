# Recover native ComfyUI admission after an interrupted submission

Status: completed

Owner: Codex

Started: 2026-10-16; updated: 2026-10-16

## Goal and observed behavior

Restore processing after E: disconnected and prevent a managed native ComfyUI
failure from holding the queue indefinitely. The running proxy uses this C:
checkout, E:\Zet\AI_Queue, and local runtime state on C:.
`Proxy._run_local_scheduler` waits indefinitely on `unsettled_comfyui.json`, even
after restart has stopped the previous owned backend. There were 22 queued image
jobs, no running file worker, no ComfyUI process and a refused upstream connection.
The stale guard was archived locally; the existing proxy began backend startup.

## Desired behavior and impact

Before new local admission, a managed runtime with a native unresolved guard must
stop its owned ComfyUI backend and verify the endpoint is gone using the existing
`BackendManager.stop_comfyui` contract. Then archive the guard and continue.
Failed/interrupted attempts keep their terminal receipts and are never replayed.
External backend ambiguity remains blocked; never terminate an unowned process.

Code scope: [engine.py](../../file_proxy/engine.py), scheduler guard handling only.
Documentation scope: native providers, managed runtime, data model, invariants,
code map, development and a focused ADR. No dependencies or test scenarios added.
Existing HTTP settlement, cache publication, cloud capacity, immutable envelopes
and [execution invariants](../../Docs/INVARIANTS.md) remain intact.

## Luna-sized work packets

1. **Reconcile the managed native guard**
   - Own `Proxy._run_local_scheduler` and one recovery helper in engine.py.
   - Depends on observed guard and existing backend stop/ownership inspection.
   - Stop/verify before archiving; retain the guard on failure; log transitions
     without flooding repeated failures. Do not alter terminal receipts.
   - Checkpoint: managed admission can resume only after verified stop; external
     runtimes retain the existing hold. Validate with existing E2E suite and diff.
2. **Document recovery and verify resumed processing**
   - Depends on packet 1. Update only the affected contract/operation documents
     and add ADR 0005 for automatic managed recovery.
   - Read live status/logs to verify the already-running proxy progresses after
     the operational guard archive; do not start a second proxy or replay jobs.
   - Checkpoint: checks recorded, document links valid, final diff reviewed.

## Validation and limitations

Run `.\.venv\Scripts\python.exe -m pytest -q` and `git diff --check`.
Do not add/expand tests without approval per [AGENTS.md](../../AGENTS.md).
The existing suite does not directly exercise the new native-guard recovery path;
record that limitation. The running proxy does not reload edited engine code until
its process restarts; operational recovery is separate from code validation.

## Progress

- Operational stale guard archived as
  `unsettled_comfyui.recovered-20261016.json`; live status became unblocked and
  backend startup began. No published job envelope was changed.
- Packet 1: completed. The scheduler uses ownership-aware verified stop before
  archiving a native guard; repeated identical recovery failures are logged once.
- Packet 2: completed. Updated component/data/invariant/code-map/development/index
  documents and added ADR 0005. Live ComfyUI history reported a completed successful
  render, then another image job was running; queued file work fell from 22 to 20.

## Final results

- Existing isolated suite: 6 passed, 1 live scenario deselected (8.46s).
- `git diff --check` passed. Final source/document diff and Markdown links reviewed.
- New recovery branch has no dedicated scenario; no tests were added/expanded.
- The existing active process remains running with the operational guard cleared.
  The code change requires the next full application launch; dashboard runtime
  restart does not reload Python modules. No destructive cleanup, replay, external
  publication, dependency changes or subscriber code edits were needed.
