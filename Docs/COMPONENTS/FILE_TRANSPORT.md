# Filesystem transport and one-job workers

## Responsibility and interfaces

The proxy validates eventually synced envelopes, runs one trusted local worker,
and publishes a terminal answer. Subscriber business logic and harvesting remain
outside this repo. Primary implementation: [engine.py](../../file_proxy/engine.py),
[validation.py](../../file_proxy/validation.py), [registry.py](../../file_proxy/registry.py).
Formats are owned by [DATA_MODEL.md](../DATA_MODEL.md); publisher/harvester examples
by [README.md](../../README.md).

Inputs: immutable `Ask/<subscriber>/<job>` with `job.json` and app payloads;
trusted registry and process environment. Outputs: `Answer/<subscriber>/<job>`
with `proxy_result.json` and complete non-hidden inventory. Public engine methods
are `Proxy.once/run/process_once/status`; the async scheduler also calls selection
and execution internals. `process_once` assumes its caller owns runtime coordination;
`once` and `run` acquire the lock/recover first.

## Workflow

1. Subscriber creates hidden staging, writes/inventories material payloads, writes
   manifest last, and publishes via same-filesystem rename. Hidden candidates are
   ignored; no publisher service is implemented here.
2. `_select_file_job` first republishes completed Running outcomes, then `_scan`
   validates visible asks and registry membership. A process-local cache reuses
   successful or incomplete validation while the envelope and directory-entry
   identities, sizes, and timestamps remain unchanged. Status reads inspect this
   metadata and cached diagnostics without hashing payload bytes. A missing/malformed manifest
   or declared file mismatch raises `JobNotReady`: first-seen monotonic time is
   remembered in memory until sync grace expires. Restart resets that timer.
   Invalid contracts and malformed other JSON become `INVALID` answers.
3. Selection returns either `QueueJob` or an invalid-job tuple. Invalid publication
   does not acquire/start a backend. Valid ordering follows [SCHEDULING.md](SCHEDULING.md).
   A changed entry invalidates cached validation; disappeared jobs are evicted.
4. Immediately before moving Ask to Running, execution performs a fresh full checksum
   validation and requires the manifest to match the selected identity. Async managed
   execution prepares backend first; `_process_file_job` moves ask
   to Running, prepares Forge when needed, records resource affinity, and scans
   the file queue for advisory next-resource hints.
5. `subprocess.run` uses registry argv + `--job-dir <absolute Running path>`,
   configured cwd/timeout, `shell=False`, full stdout/stderr capture, inherited
   environment plus three hints: `AI_PROXY_CURRENT_RESOURCE_KEY`,
   `AI_PROXY_NEXT_JOB_PRESENT` (`1`/`0`), `AI_PROXY_NEXT_RESOURCE_KEY`.
   Hints observe filesystem asks only; they are not reservations and do not
   predict queued HTTP requests.
6. Worker exit/timeout/launch failure maps to a terminal result. Missing job folder
   is recreated with minimal identity metadata and failed; otherwise envelope
   validation runs again, detecting mutated inventoried inputs and unsafe JSON.
7. Build full non-hidden inventory; persist normal-worker outcome locally;
   atomically write transport result; rename to Answer; remove local outcome
   journal. An Answer collision leaves the current envelope in place, is reported
   as blocked, and does not overwrite the prior Answer or replay the worker.
   Invalid-job publication follows the same collision handling.

The cache is process-local and disposable. Storage interruption/reconnect clears it;
restart also begins cold. It is only a scan/status optimization and never authorizes
execution. Post-worker validation and complete output inventory remain unchanged.
8. Subscriber validates full result inventory, copies artifacts durably, applies
   business updates idempotently, then owns retention. Queue listings alone are
   not an authoritative business state machine.

## Recovery, error handling, and constraints

`recover_interrupted` examines Running at startup: restore local journal into
missing result, publish an existing result unchanged, otherwise publish FAILED
`PROXY_INTERRUPTED`. It never reruns the worker. `_publish_completed_running`
also retries already-recorded outcomes during polling. There is still a crash
window after an external side effect and before a durable outcome; workers need
idempotent effects per `(subscriber_id, job_id)`.

Atomic replacements retry `PermissionError` with backoff for 15 seconds;
inventory reads retry `OSError` for 15 seconds. Async scheduling records filesystem
degradation for handled `OSError` paths while listeners remain up. Synchronous
mode is not equally resilient to every moving/locked path; do not assume all scan,
status, collision, or malformed recovery errors are caught. See development gaps.

Registry commands are trusted code and are not sandboxed. Recursive path checks
cover recognized JSON references; symbolic links and Windows reparse points are
removed from invalid envelopes before a regular `INVALID` answer is published,
without traversing or changing their targets.
This is not a general sandbox for arbitrary worker I/O.
Worker contract: leave manifest/inputs intact, keep outputs inside envelope,
close complete outputs before exit 0, write useful bounded diagnostics, do not
poll/retry/move jobs. Captured logs are not truncated by the proxy.

## Extension points and verification

Add subscriber workers in local registry files, not in job-provided commands.
Payload schema and prompt changes belong in the subscriber worker; only extend
the manifest when transport/scheduling needs it. Inventory/identity changes affect
publishers, workers, harvesters and recovery together.

[File task scenario](../../tests/test_e2e.py) covers delayed inventory sync,
subprocess success/failure, worker-output path validation, and published answer
inventory. The dashboard scenario in the same module verifies interrupted
recovery without replay and safe worker shutdown. Priority combinations,
sync-grace expiry and journal crash windows are not separate scenarios.

Related contracts: [INVARIANTS.md](../INVARIANTS.md). Related reconstructed choice:
[transport/local-state ADR](../DECISIONS/0001-transport-and-local-state.md).

## Native provider payloads (2026-10-16)
Native `ai_*` registrations execute `ai_request.json` version 1 while preserving the
protocol-v1 envelope/inventory. Result interpretation/application remains with the
subscriber. Legacy registrations remain for existing immutable asks. See
[native providers](AI_PROVIDERS.md) for contracts, cloud scheduling and model metadata.

## Removable-volume recovery (2026-10-17)
Daemon, filesystem-only, and supervisor startup keep polling a disconnected queue. Health uses fixed-local identity evidence and two successful probes separated by two seconds. Worker hosts journal process identity/exit state before engine publication; known successful outcomes are published after reconnect. No missing job directory is recreated. Interrupted/storage-failed executions carry optional retry classification, but only subscribers create fresh attempts. Status retains known rows and uses null counts during unavailability. See [ADR 0006](../DECISIONS/0006-storage-outage-recovery.md).

Surviving worker hosts retain their execution lane/capacity until exit, including
when the queue is temporarily absent. Native or witnessed successful outcomes are
revalidated before publication; genuine known provider errors are not relabeled as
storage failures merely because an outage overlaps them. Probes have one worker
per monitor and a bounded status wait; root identity evidence is exclusive across
local startup processes.
