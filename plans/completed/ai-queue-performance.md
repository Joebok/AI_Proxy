# AI_Proxy queue validation performance

Status: active
Owner: Codex
Started: 2026-10-26
Related: cross-repository AI Queue remediation; see the Zet active implementation plan.

## Goal

Reduce repeated reads and hashes of unchanged filesystem-job inputs in Proxy scheduling and dashboard status, especially large images on removable storage. Preserve full inventory integrity and all existing execution/sync/recovery semantics.

## Current behavior

- `Proxy._scan()` calls `validate_job_folder()` every polling cycle. `validate_file_inventory()` rereads and SHA-256 hashes every inventoried input.
- The cloud dispatcher independently scans every poll; local scheduling rescans to select and emit next-resource hints. `Proxy._available_status()` performs another full validation of Ask/Running jobs on status refresh.
- Worker completion validates the envelope again, builds an output inventory and durably records its outcome.
- Live investigation captured repeated validation in scheduler/status stacks during a user-started image batch.

## Desired behavior

Repeated discovery/status reuses successful and incomplete-transfer validation for unchanged waiting inputs. Changed files invalidate the entry. Each selected job receives a fresh, uncached full inventory/hash validation before it is claimed for execution; its selected manifest identity must still match. Post-worker validation, output inventory, sync grace, ordering, collision and interruption recovery are preserved.

## Impact analysis

| Files/modules | Interfaces / consumers | Checks / documentation |
| --- | --- | --- |
| `file_proxy/engine.py` | `Proxy` local/cloud scheduler scans, status, job claim and cache lifecycle | Existing file/scheduler/dashboard/restart scenarios; disposable repeated-read/invalidation checks; File Transport, invariants, development |
| `file_proxy/validation.py` | Inventory and immutable envelope validation, cache signature helper if colocated | Existing delayed-sync/input-mismatch checks; same-size content mutation, metadata changes, eviction/reconnect disposable checks |
| `tests/test_e2e.py` | Existing five default end-to-end scenarios only | No permanent coverage additions without separate approval; only user-approved temporary checks |

## Invariants and constraints

- Keep protocol-v1 published envelopes immutable in behavior; do not weaken inventoried-byte verification.
- Require fresh checksums immediately before worker claim and retain existing post-worker full validation/output inventory.
- Preserve path/reparse-point safety, manifest identity, local/cloud scheduling, non-preemptive execution, storage identity recovery and no-worker-replay contract.
- Cache is process-local and disposable; it stores only metadata, validation results and diagnostics, never file contents.
- Do not start/stop the live proxy, consume user jobs, start ComfyUI/Ollama, install dependencies or add maintained tests.

## Implementation steps

### WP1 — Reuse validation for unchanged Ask inputs
- Depends on: none.
- Files/interfaces: `file_proxy/engine.py`, `file_proxy/validation.py`; new private process-local cache shared by all Proxy scan callers.
- Work: derive a stable signature from envelope plus per-entry file identity/size/timestamps; cache successful and incomplete/invalid diagnostics; retain existing first-seen sync-grace timers. Metadata-only status must skip payload hashing for Ask and Running. Evict removed jobs and invalidate fingerprints after storage disconnect/reconnect/root identity changes. Add uncached full validation before Ask→Running claim, require the admission manifest to equal the selected manifest, then preserve post-worker validation.
- Acceptance: repeated unchanged queue/status scans do not reread input bytes; any changed input is rechecked, and stale/mismatched bytes cannot reach a worker.
- Validation: existing file task, scheduler, dashboard/restart and settlement scenarios; approved disposable reads/bytes check with large input, same-size mutation, replacement, removal, restart, disconnect/reconnect.
- Status: pending.

### WP2 — Record validation lifecycle and completion
- Depends on: WP1.
- Files/interfaces: File Transport, INVARIANTS, DEVELOPMENT and this active plan.
- Work: document signature invalidation and cache limits without adding dependency or changing envelope format. Run the complete isolated default E2E suite, disposable checks and source/diff inspection; record results and gaps, archive this plan only after passing acceptance.
- Acceptance: unrelated HTTP scheduling/cache-before-history/answer publication contracts remain unchanged; resulting diff contains only performance work and its docs.
- Validation: `.\.venv\Scripts\python.exe -m pytest -q`, Python compilation/syntax and `git diff --check`.
- Status: pending.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m py_compile file_proxy\engine.py file_proxy\validation.py
git diff --check
```

The existing suite uses temporary queue roots and fake endpoints/workers; it does not scan the production queue. Run approved disposable instrumentation only against a temporary queue under an isolated temp directory. Do not invoke `once`, live `status`, `run`, or `tui` against the real queue.

## Risks and alternatives

- Metadata signatures may miss intentionally preserved timestamps; fresh admission hashing makes the selected execution safe. Cache misses only cost another scan/hash.
- Atomic file replacement can race a scan. Cache only when the before/after signature remains stable; otherwise retry on the next scan.
- Preserve `_not_ready_since` behavior and diagnostic classification when cached.
- Process restart and successful storage reconnect force revalidation; do not persist cache entries.

## Progress and decisions

| Packet | Status | Notes |
| --- | --- | --- |
| WP1 | complete | `FileValidationCache` uses entry metadata for scan/status reuse; `_execute_file_job` performs uncached full validation before claim. Final isolated suite: 7 passed, 1 deselected; disposable changed-input, replacement, restart and invalidation checks passed. |
| WP2 | complete | File transport, invariants and development notes updated; Python compile and `git diff --check` passed. |

## Final results

`\.venv\Scripts\python.exe -m pytest -q`: **7 passed, 1 deselected**. `py_compile` for `engine.py`/`validation.py` and `git diff --check` passed. A temporary folder check confirmed warm unchanged reuse, same-size replacement invalidation, changed-input rejection and revalidation after a new cache instance. No real queue, live model/backend, Python 3.10 runtime or physical E: reconnect was exercised. Documentation: [File Transport](../../Docs/COMPONENTS/FILE_TRANSPORT.md), [invariants](../../Docs/INVARIANTS.md), and [development](../../Docs/DEVELOPMENT.md).
