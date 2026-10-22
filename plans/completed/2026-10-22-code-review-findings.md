# Address verified code review findings

Status: completed

Owner: Codex

Started: 2026-10-22; updated: 2026-10-22

## Goal

Correct the verified safety, containment, admission accounting, restart,
publication, retention, and documentation issues from
`Docs/CodeReview/CodeReview.md`. Preserve single-attempt execution, immutable
published envelopes, the managed/external ownership boundary, and the existing
test approval limit. No new tests or expanded scenarios are authorized.

## Current Behavior

The review findings were checked against HEAD
`1ebedb28efca843bdafa8294101cd96e1381e298`. Native guard checks can precede a
selection/recovery step that creates a guard; `once` lacks the guard check.
Validation and inventory rely on `Path.is_symlink`, invalid publication
inventories before sanitizing, HTTP body normalization can fail after reserving
bytes, and several recovery/collision paths can terminate or misattribute work.
The source advertises Python 3.10 but imports `datetime.UTC`. Local execution
sidecars lack a cleanup/attempt-generation policy. Documentation defaults and
scenario counts disagree with source and other current docs.

## Desired Behavior

Unresolved or unreadable native submission evidence blocks local execution until
safe PREPARED clearing or ownership-aware verified recovery. Queue jobs cannot
read/write through link-like paths outside the queue, and invalid publication
does not expose or alter their targets. Admission capacity is released exactly
once. Restart uncertainty remains explicit and uses backend-specific evidence.
Collisions and corrupt per-job state remain isolated. Durable local metadata has
a bounded lifecycle and cannot leak between reused attempt IDs. Support and
deployment documentation match the executable contract.

## Impact Analysis

| Files/modules | Interfaces/data/consumers affected | Tests and documentation |
| --- | --- | --- |
| `engine.py`, `backend.py` | File and HTTP local admission, guard recovery, Answer publication | Existing scheduler, native and dashboard scenarios; invariants and component docs |
| `validation.py`, `ai_worker.py`, `http_proxy.py` | Queue paths, inventories, managed outputs, HTTP admission/lifecycle | Existing file and HTTP scenarios; data model and transport docs |
| `storage_health.py`, `store.py`, `worker_host.py` | Local journals, prompt uniqueness, cache/history | Existing restart and ComfyUI scenarios; data model docs |
| `README.md`, `AGENTS.md`, `Docs/DEVELOPMENT.md`, component docs | Python support, launcher defaults, test-count and recovery claims | Link and source-reference checks |

## Invariants and Constraints

- Follow `Docs/INVARIANTS.md`, especially immutable receipts, safe relative
  paths/inventory, no worker replay, non-preemptive scheduling, backend
  ownership, durable guard semantics and cache-before-history publication.
- No new production dependencies.
- No tests or test-scenario expansion without Joe's explicit approval. The
  checkout has six E2E scenarios while the supplied AGENTS instructions state
  five approved scenarios; implementation validation may run existing tests.
- Preserve the pre-existing untracked `Docs/CodeReview/` report.

## Implementation Steps

1. **Packet A: Enforce guard decisions at admission.**
   - Owned paths: `file_proxy/engine.py`, relevant invariant/recovery wording.
   - Changes: centralize guard state; handle unreadable state as blocked; check
     after recovery/selection and immediately before local file or queued HTTP
     execution; keep cloud lane and verified managed stop behavior.
   - Preserve: PREPARED can be archived without backend mutation; ambiguous
     submissions are never auto-cleared; no execution replay.
   - Checkpoint: one-shot and scheduler paths cannot start local work while an
     ambiguous or unreadable guard exists.
   - Validation: existing scheduler/recovery scenarios and isolated manual
     guard check; no new test branch.

2. **Packet B: Contain Windows link-like queue paths.**
   - Owned paths: `validation.py`, `engine.py`, `ai_worker.py`, managed output
     check in `http_proxy.py`.
   - Depends on: Python-version-compatible detector.
   - Changes: reject reparse points at roots and within inventoried job trees;
     check containment before inventory and publication; invalid linked jobs
     publish regular INVALID answers without traversing or writing through the
     link.
   - Checkpoint: external targets are unchanged and absent from inventories.
   - Validation: disposable symlink/junction check and existing file scenario.

3. **Packet C: Release HTTP admission reservations exactly once.**
   - Owned paths: `http_proxy.py`.
   - Changes: explicit reservation ownership across body read, normalization,
     enqueue and lifecycle task; controlled encoding/normalization client error.
   - Checkpoint: failure/cancellation restores request and byte counts.
   - Validation: existing HTTP scenario plus an isolated manual fault check.

4. **Packet D: Make the declared Python minimum importable.**
   - Owned paths: `reasoning_policy.py`, `README.md`, `Docs/DEVELOPMENT.md`,
     setup guidance if needed.
   - Changes: use `timezone.utc` while retaining Python >=3.10; align claims.
   - Checkpoint: CLI import/help succeeds on 3.10.
   - Validation: run on an installed 3.10 interpreter if available; otherwise
     record that minimum-version runtime validation remains unverified.

5. **Packet E: Preserve ComfyUI uncertainty on restart.**
   - Owned paths: `http_proxy.py`, `backend.py`, HTTP transport docs.
   - Changes: never probe an unrelated upstream; keep pending ComfyUI records
     blocked until terminal history or verified owned-process termination.
   - Checkpoint: listener absence or delayed history does not falsely terminalize
     uncertain work.
   - Validation: existing ComfyUI lifecycle scenario and isolated inspection.

6. **Packet F: Isolate publication collisions.**
   - Owned paths: `engine.py`, file transport docs.
   - Changes: classify collision at publication boundary, preserve Running data,
     log identity and continue scheduler without overwrite or replay.
   - Checkpoint: prior Answer remains byte-for-byte unchanged and scheduler lives.
   - Validation: disposable filesystem check; no new scenario.

7. **Packet G: Bound local execution metadata.**
   - Owned paths: `engine.py`, `ai_worker.py`, `worker_host.py`, data model docs.
   - Changes: associate native sidecars with an attempt generation; clean only
     metadata no longer needed for live recovery; define retention.
   - Checkpoint: reused IDs cannot consume stale result/submission evidence.
   - Validation: disposable reused-ID and publication checks; preserve in-flight
     recovery evidence.

8. **Packet H: Keep catalog refresh alive.**
   - Owned paths: `engine.py`.
   - Changes: guard request-file removal and retry transient failures; close the
     catalog worker on shutdown if bounded joining is safe.
   - Checkpoint: unlink failure does not terminate future refreshes.
   - Validation: isolated manual failure check.

9. **Packet I: Reject duplicate returned prompt IDs consistently.**
   - Owned paths: `http_proxy.py`, `store.py` as needed, HTTP transport docs.
   - Changes: validate before assigning the lifecycle ID; stop reconciliation
     and archival against an ID owned by another execution.
   - Checkpoint: both records and artifacts retain correct ownership.
   - Validation: existing ComfyUI scenario and disposable store/lifecycle check.

10. **Packet J: Isolate corrupt local JSON.**
    - Owned paths: `storage_health.py`, `engine.py`, `ai_worker.py`.
    - Changes: define parse policy per journal; unreadable guard blocks; other
      corruption is reported and isolated to the affected job where safe.
    - Checkpoint: startup/scheduler remain operable without discarding safety
      uncertainty.
    - Validation: isolated corrupt-file checks.

11. **Packet K: Synchronize docs and close the plan.**
    - Owned paths: affected docs and this plan.
    - Changes: correct scenario counts, launcher default, support/recovery and
      metadata behavior; resolve the five-versus-six approval wording without
      expanding tests.
    - Checkpoint: source references and Markdown links resolve; docs match code.
    - Validation: smallest existing suite, CLI help, `git diff --check`, link
      check, final diff review.

## Validation

Run the project venv CLI help and the existing E2E suite; run isolated temporary
filesystem checks for junction containment, guard blocking, publication
collision and local-state corruption. Do not use a real queue or live backend.
No added/expanded tests. Run `git diff --check` and validate relative Markdown
links. Record exact outcomes below.

## Risks and Alternatives

The review report's CR-002/CR-003 acceptance text conflicts about publishing an
Answer for a link-like job. This plan chooses a regular terminal INVALID Answer
with link entries removed without traversing or changing their targets,
consistent with invalid-envelope publication. Rejecting every reparse point in
a job tree is stricter than the existing symlink escape-only rule; the contract
now states that boundary. The current test inventory has six scenarios; the
five-scenario statement in AGENTS records the approved baseline, while the sixth
is identified as separately approved in native-provider documentation. Python
3.10 is not installed on this host, so minimum-version runtime validation is
unavailable. CR-006 and CR-010 include backend/upstream-dependent cases that
cannot be proven with the default isolated suite.

## Progress and Decisions

| Packet | Status | Discoveries, deviations, remaining work |
| --- | --- | --- |
| A | complete | One-shot and scheduler share the guard fence; unreadable/link-like guards block and cannot be auto-recovered. |
| B | complete | Reparse detection precedes inventory; invalid publication removes link entries without reading or changing targets. |
| C | complete | Normalization errors and pre-start lifecycle cancellation release admission reservations. |
| D | complete | Python 3.10-only API removed; runtime validation on 3.10 unavailable. |
| E | complete | ComfyUI reconciliation uses only ComfyUI; managed unresolved work requires verified stop. |
| F | complete | Answer collisions preserve prior results, leave current work visible as blocked, and do not replay. |
| G | complete | Execution evidence is removed after publication and stale sidecars are cleared before a fresh attempt. Cleanup failures are logged. |
| H | complete | Catalog request unlink failures are contained; catalog thread shutdown uses a bounded join. |
| I | complete | Duplicate returned IDs do not change the tracked ID or receive cross-attributed cache data; backend admission remains blocked. |
| J | complete | Corrupt guards fail closed; corrupt health journals report RECOVERY_REQUIRED; corrupt job recovery evidence is isolated or held blocked. |
| K | complete | Contracts, setup/launcher guidance and validation record were synchronized; links resolve. |

## Final Results

Implemented CR-001 through CR-011. Updated:
`file_proxy/engine.py`, `file_proxy/validation.py`, `file_proxy/http_proxy.py`,
`file_proxy/storage_health.py`, `file_proxy/ai_worker.py`,
`file_proxy/reasoning_policy.py`, `README.md`, `Docs/DEVELOPMENT.md`,
`Docs/INVARIANTS.md`, `Docs/DATA_MODEL.md`, and the file/HTTP/native-provider
component docs. No tests were added or expanded. The pre-existing untracked
`Docs/CodeReview/` report was preserved.

Observed validation:

- `.\.venv\Scripts\python.exe -m compileall -q file_proxy` — passed.
- `.\.venv\Scripts\python.exe -m pytest -q` — 6 passed, 1 deselected.
- Disposable junction/invalid-publication, guard-state, corrupt-storage-journal,
  and Answer-collision checks — passed.
- `git diff --check` — passed; Git reported only line-ending conversion notices
  for four edited files.
- Relative Markdown links in 11 changed/relevant documents — all resolved.
- Python 3.10 runtime validation — not available; Python 3.10 is not installed.
- Live backends — not run. Upstream duplicate-ID and restart verification branches
  remain limited to static review and isolated local checks.
