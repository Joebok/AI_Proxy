# Shared execution scheduling

## Responsibility and terms

[engine.py](../../file_proxy/engine.py) selects filesystem jobs and arbitrates
HTTP/file dispatch; [scheduler.py](../../file_proxy/scheduler.py) owns `HttpQueue`
and `HttpJob` futures. Backend preparation is delegated to `BackendManager`.
**Affinity** prefers reusing a resource key to reduce transitions; it is bounded
and does not allow overtaking an earlier different backend block. This describes
current working-tree code, including existing uncommitted ordering fixes.

Inputs: validated file jobs and admitted HTTP jobs. Output: one selected expensive
operation and its start/finish handshake. Consumers: HTTP handlers/prompt tasks,
filesystem subprocess runner, managed backend lifecycle, dashboard queue snapshot.

## File selection

`Proxy._scan` sorts by descending `priority`, then ascending UTC `created_at`,
`subscriber_id`, `job_id`. Invalid jobs are answered ahead of valid selection.
`_choose_resource_job` considers only the contiguous leading block with the same
resolved backend as the first job, then only its highest priority:

- Without an active resource key: choose the first eligible job.
- Below `max_resource_streak`: choose first matching resource if present.
- At the bound (or without a match): choose first different resource if present,
  otherwise first eligible.

Backend resolves from trusted registry first, then recognized `ollama:`/`comfyui:`
resource prefix. Jobs with no key reset affinity when recorded. The bound limits
preference while different work is eligible; it is not a cap on consecutive jobs
if all have the same resource. Priority can reorder creation time, so this is not
unconditional FIFO. `_next_resource_hint` uses the same file selection logic.

## HTTP queue selection

`enqueue` creates UUID request ID, `started`/`finished` futures, resource/backend,
barrier flag, loop-monotonic and wall-clock enqueue timestamps. `_candidate`
restricts affinity to the leading contiguous backend block and stops at the first
barrier, inclusive. It chooses the first job when that job is a barrier or has
waited `oldest_override_seconds`; otherwise uses bounded affinity/different-key
selection within that prefix. It never affinity-selects a job beyond a barrier.

`pop` updates **HTTP queue's own** resource streak; `peek` does not. The engine
also maintains an independent streak for shared/file selection: an HTTP start
records its resource in the engine and can affect the next file choice. Do not
collapse these state owners without validating both policies.

`cancel` marks a job and resolves its finish future; selection drops cancelled
waiting jobs. `finish` resolves finish once. `close` marks queue closed, wakes
undispatched futures and clears waiting jobs. Enqueue after close fails.

## Cross-queue arbitration and slot lifecycle

`Proxy.run_scheduler` peeks HTTP and selects file work to compare candidates.
HTTP normally wins, with one explicit exception: selected file `created_at` is
no later than HTTP enqueue wall time, both resolved backends are known/truthy,
and backends differ. In that case the older file runs first. This comparison uses
the already-selected file job, not a global merge of all job timestamps.

After HTTP work, continuation grace delays file work for a follow-up request
(default 2 s with listeners). This can also delay a selected older different-
backend file immediately after HTTP completion. Grace is the maximum configured
listener grace; managed file-only runtime uses zero. HTTP affinity is not strict
FIFO, and HTTP preference is not unconditional priority.

For HTTP, scheduler prepares backend/resource, signals `started` (or records
`start_error`), and awaits `finished`; only then releases backend and continues.
The handler owns finish for ordinary relay; a dedicated lifecycle task owns it
for ComfyUI prompt settlement. Active work is never preempted.

For files, async backend preparation precedes worker execution. Managed jobs with
no resolvable backend are held; backend failures hold the ask for a later poll.
Invalid tuples bypass backend preparation and publish their answer. Selected
valid jobs execute in a shielded thread task: cancellation waits for the worker
before propagating. File affinity updates inside execution, including jobs that
fail, rather than only on successful completion.

## Failure modes and extension points

Missing backend metadata weakens cross-queue comparison and can hold managed
work. Long-lived or unresolved prompt work blocks dispatch by design. Same-backend
HTTP traffic can defer file work; no universal file starvation bound is enforced.
File timestamps are producer-supplied while HTTP wall time is local; cross-machine
clock skew affects arbitration. Age override exists for HTTP, not file jobs.

Changes to priority/affinity/backend normalization affect worker hints, Forge
cleanup, backend process residency, HTTP/control ordering and client latency.
Update the two selection layers together and run the shared scheduler scenario.
Ask Joe before expanding that scenario or adding another test for uncovered
priority combinations.
Do not dispatch futures or release slots from a new path without explicit ownership.

## Tests and decisions

[Shared scheduler scenario](../../tests/test_e2e.py) verifies non-preemptive file
and HTTP work, streamed completion, queued follow-up requests and a responsive
bypass route. [ComfyUI lifecycle scenario](../../tests/test_e2e.py) verifies the
slot remains held through keyed history. Priority matrices, HTTP barriers and age
override do not have separate scenarios.

Related: [contracts](../INVARIANTS.md),
[shared execution ADR](../DECISIONS/0002-shared-execution-and-backend-ownership.md).
