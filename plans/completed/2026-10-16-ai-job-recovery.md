# Native AI outcomes for Zet job recovery

Approved and implemented 2026-10-16. Companion: [Zet implementation plan](../../../Zet/plans/completed/2026-10-16-ai-job-recovery.md).

## Luna-sized packet 7

- Own file_proxy/ai_worker.py and affected provider documentation; depends on the existing native request-v1 contract, independent cloud lane and terminal publication.
- Return a failing CLI exit code for native execution failures, retain structured answer evidence, report concise provider error codes (including quota failure), and persist the actual accepted ComfyUI prompt identity before polling.
- Preserve immutable envelopes, single attempts, local non-preemption, two Codex slots, and unresolved local-submission fencing. No live runtime restart or repair.
- Acceptance: failure cannot be mistaken for successful execution; unresolved ComfyUI admission remains blocked. Validate with the six existing tests in tests/test_e2e.py and git diff --check. No additional permanent tests or expanded scenarios.

## Progress

- Complete: native CLI exits nonzero after structured failure publication; Codex quota/CLI and ComfyUI terminal errors carry concise codes/messages; the accepted ComfyUI prompt ID is journaled before polling. Keyed history retains detailed evidence. Scheduling is unchanged.
- Final observed validation: `.venv/Scripts/python.exe -m pytest tests/test_e2e.py -q` **6 passed in 8.80 s**; changed-source AST parsing and `git diff --check` passed. No tests added/expanded; no live provider check or runtime restart performed.
- Native registry commands load the worker module from this checkout; subsequent workers can observe edits without a scheduler restart. A deliberate consistent-runtime restart remains a separate operator action. Old jobs were not repaired, following the user's clarification.
