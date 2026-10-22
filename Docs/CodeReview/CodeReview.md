# AI Proxy Independent Code Review

## 1. Review baseline

- Repository: `C:\Users\Joe\Projects\CodeReview\AI_Proxy`
- Branch: `main`
- HEAD: `1ebedb28efca843bdafa8294101cd96e1381e298`
- Review date: 2026-10-22
- Mode: read-only independent review. No application code, configuration, registrations, queue data, or tests were modified. The only file added by this review is this report.
- Scope: transport ownership, queue scheduling, local execution, HTTP/ComfyUI/Ollama handling, managed and external backend ownership, restart recovery, storage handling, CLI/TUI/setup behavior, documentation contracts, and the maintained test suite.
- Baseline state: clean working tree (`git status --short` produced no output) and no pre-existing `Docs/CodeReview.md`.
- Sources reviewed: `Docs/INDEX.md`, `Docs/ARCHITECTURE.md`, `Docs/CODE_MAP.md`, `Docs/DATA_MODEL.md`, `Docs/INVARIANTS.md`, `Docs/DEVELOPMENT.md`, all component documents, all six ADRs, `README.md`, `AGENTS.md`, launchers, setup scripts, `runtime-config.example.json`, and `pyproject.toml`.
- Code reviewed: all modules under `file_proxy/`, including `engine.py`, `http_proxy.py`, `scheduler.py`, `store.py`, `validation.py`, `models.py`, `backend.py`, `ai_worker.py`, `worker_host.py`, `storage_health.py`, `registry.py`, `runtime.py`, `supervisor.py`, `model_catalog.py`, `reasoning_policy.py`, `diagnostics.py`, `cli.py`, and `tui.py`.
- Tests reviewed: `tests/conftest.py`, all six scenarios in `tests/test_e2e.py`, and the opt-in `tests/test_live_backends.py`.
- The required validation command could not be run in this checkout: `.\.venv\Scripts\python.exe` does not exist. The available system Python is 3.14.6 and does not have `aiohttp`; dependencies were not installed because this review is read-only.
- Controlled filesystem experiments were run outside the repository to verify Windows junction behavior. They did not modify the repository.

## 2. Executive assessment

### Overall assessment

The design is coherent and unusually explicit for a transport scheduler. The separation between subscriber-owned application work and proxy-owned transport/scheduling, the immutable Ask/Answer envelopes, trusted registry command registration, single-attempt execution, non-preemptive local scheduling, cache-before-history publication, and managed/external backend distinction are all represented in code and documentation.

The most important issue is that the native ComfyUI "unsettled submission" guard is not a single atomic admission predicate. `once` does not check it at all, and the scheduler/`process_once` path checks it before a selection step that can create the guard during recovery and then continue selecting and executing local work. This can allow a new local attempt to start while an earlier native ComfyUI submission is still unresolved, which is the central safety invariant of the recent fault-tolerance work.

No P0 issue was confirmed. The remaining material issues are Windows reparse-point containment, an admission-accounting leak on an exception path, a declared Python minimum that the CLI cannot satisfy, and several restart/resilience/operational gaps. Runtime confidence is limited because the maintained pytest suite could not be executed in this checkout.

### Priority summary

| Priority | Count | Theme |
| --- | ---: | --- |
| P0 | 0 | No unconditional data-loss or immediate unsafe execution path confirmed by static review. |
| P1 | 1 | Native ComfyUI guard can be bypassed at or after local-job selection. |
| P2 | 4 | Windows junction containment, invalid-path link handling, admission byte leak, Python 3.10 incompatibility. |
| P3 | 6 | Restart reconciliation assumptions, Answer collision fail-stop, local execution-artifact growth, cloud catalog thread death, duplicate prompt-ID attribution, corrupt local JSON handling. |

### Strengths worth preserving

- Ask envelopes are treated as immutable and are moved into `Running` before work begins; successful results are written into `Answer` by replacement rather than mutation.
- Worker commands come from a trusted registry and are launched with `shell=False`; the proxy does not import subscriber code.
- Local scheduling is serialized and non-preemptive; HTTP work has its own bounded queue and an independent cloud lane.
- The managed ComfyUI process uses Windows job containment and an explicit ownership distinction; external ComfyUI is not silently adopted.
- Cache files are written before history rows are exposed, and duplicate managed outputs are removed.
- Storage health distinguishes an unavailable queue from an empty queue; local process witnesses and native submission sidecars support recovery without replay.
- The documentation is detailed, cross-referenced, and largely honest about unverified live behavior. All Markdown links checked during this review resolved.

## 3. Prioritized findings

### CR-001 - Native ComfyUI admission guard is not enforced at the actual local-execution boundary

- Priority: P1
- Confidence: high from static tracing; no live native ComfyUI run was possible.
- Locations:
  - `file_proxy/engine.py:990-995` (`once`)
  - `file_proxy/engine.py:709-718` (`process_once`)
  - `file_proxy/engine.py:851-966` (local scheduler), especially the guard check at `860-861` and selection at `924`
  - `file_proxy/engine.py:437-456` (`_select_file_job`)
  - `file_proxy/engine.py:458-477` (`_publish_completed_running`)
  - `file_proxy/engine.py:310-360` (`recover_interrupted`)
  - `file_proxy/engine.py:720-739` (`_preserve_unsettled_backend`)
  - Contracts: `Docs/INVARIANTS.md:51`, `Docs/COMPONENTS/AI_PROVIDERS.md:41-48`, ADR 0005.

- Trigger:
  1. A proxy crashes, or is killed, while a native ComfyUI worker has a durable submission sidecar whose phase is not proven pre-POST. On restart `recover_interrupted()` can write `unsettled_comfyui.json` from `_preserve_unsettled_backend()`.
  2. The operator runs the `once` command. `once` calls `recover_interrupted()` but never calls `_clear_unsubmitted_guard()`, never checks `unsettled_comfyui.json`, and calls `_select_file_job(include_cloud=True)`. It therefore can select an ordinary local Ollama or ComfyUI file job and execute it while the ambiguous native submission remains unresolved.
  3. In the continuous scheduler/`process_once` path, the guard is checked before `_select_file_job()`. If the guard did not exist yet, `_select_file_job()` can enter `_publish_completed_running()`/`recover_interrupted()` and create the guard, then continue scanning and return an eligible local job. The scheduler proceeds to `_process_file_job()` without another guard check.
  4. `once` also does not construct a `BackendManager`, so it cannot perform the managed verified-stop path documented for scheduler recovery. That limitation is documented in `Docs/ARCHITECTURE.md` and `Docs/DEVELOPMENT.md`, but it does not justify running new local work while the guard is unresolved.

- Evidence:
  - `once`: `recover_interrupted(); selected = self._select_file_job(include_cloud=True)`; no guard test.
  - `process_once`: guard check at lines 713-714; selection at line 715.
  - `_select_file_job`: calls `_publish_completed_running()` in a loop before `_scan()`.
  - `_publish_completed_running`: calls `recover_interrupted()` at line 471 when a completed Running job has no result.
  - `recover_interrupted`: calls `_preserve_unsettled_backend(job_dir)` at line 354.
  - `_preserve_unsettled_backend`: writes `unsettled_comfyui.json` at lines 725-726 or 735.
  - The scheduler guard check is only at lines 860-861; execution is later at 955 and 966.

- Impact:
  - A local GPU worker can start while an earlier native ComfyUI execution is unresolved or still running. This violates the durable local-admission fence and can cause GPU/VRAM contention, duplicate local load, or work on a backend whose prior prompt ownership is unknown.
  - The operator-facing `once` command was likely intended as a safe one-shot processing path; it is instead the least protected path.
  - The continuous path can bypass the guard on the same iteration that recovery creates it, so the queue can start work even without a subsequent operator action.

- Contract:
  - `Docs/INVARIANTS.md` requires an unresolved native ComfyUI submission to remain durably guarded and to block local execution; external backend resolution requires intervention.
  - ADR 0005 requires managed recovery to stop/verify owned work before archiving the guard; a pre-POST PREPARED guard may be cleared without backend mutation.

- Recommended remediation:
  - Introduce one reusable admission predicate, for example `_local_admission_blocked()`, that checks the durable guard, orphan state, and any relevant backend ownership state.
  - Enforce that predicate immediately before launching a local worker, after all recovery/selection steps, not only at the start of a loop.
  - Make `once` use the same pre-execution check. Either have it call `_clear_unsubmitted_guard()` for PREPARED only and refuse all local work while an ambiguous guard exists, or route `once` through the same guarded selection function.
  - Keep PREPARED clearing separate from ambiguous-DISPATCHED/ACCEPTED handling. Do not auto-clear a guard merely because a new selection cycle started.
  - Make the local-execution decision and worker launch a single critical section so recovery cannot create a guard between the check and launch.

- Acceptance evidence:
  - With an ambiguous native submission sidecar and a competing eligible local Ask job, neither `once` nor a fresh scheduler start launches the local worker.
  - The guard remains present and the local job remains unpublished.
  - A PREPARED sidecar can still be cleared/archived without a backend mutation and without indefinitely blocking safe work.
  - A managed recovery still calls the ownership-aware stop path before archiving the guard.
  - Any added or expanded test requires Joe's approval under the current test policy; the existing guard-recovery scenario is the natural basis for a focused extension.

### CR-002 - Windows junctions/reparse points bypass path containment

- Priority: P2
- Confidence: high. Windows semantics were verified experimentally on this host.
- Locations:
  - `file_proxy/validation.py:94-96` rejects only `job_dir.is_symlink()`.
  - `file_proxy/validation.py:108-114` rejects only `path.is_symlink()` inside the job tree.
  - `file_proxy/validation.py:15-27` inventories with `job_dir.rglob("*")` and does not reject links.
  - `file_proxy/engine.py:398` skips only a subscriber directory that `is_symlink()`.
  - `file_proxy/engine.py:304` checks only `path.is_symlink()` before replacing an invalid path.
  - `file_proxy/ai_worker.py:52-58` validates `root / name` using `Path.resolve()` against `root.resolve()`.
  - `file_proxy/http_proxy.py:288` checks only `target.is_symlink()` for managed outputs.

- Trigger:
  - On Windows, a queue writer creates a directory junction with `cmd /c mklink /J` (no administrator privilege required), pointing `Ask/<sub>/<job>` at an outside directory that contains a valid `job.json`.
  - `Path.is_symlink()` returns `False` for a junction. `validate_job_folder()` accepts it as an ordinary job directory.
  - `job_dir.resolve()` resolves to the outside target, so `validate_relative_reference()` and `ai_worker.local_file()` accept references inside the target as "inside the job."
  - `build_file_inventory()` follows the junction and inventories external files.
  - The engine can execute a worker with the external job as its working context, and `_move_to_answer()` can publish the junction itself as the Answer path.
  - A nested junction inside a job is caught by the resolved containment check, but a junction at the job root changes the meaning of the root and bypasses that check.

- Evidence:
  - Controlled experiment created a target directory containing `job.json` and `secret.txt`, then created a junction at `Ask/demo/job`.
  - Observed: `is_symlink False`, `is_junction True`, `validate accepted: job`, inventory included `('job.json', 137)` and `('secret.txt', 14)`, and `(job / 'secret.txt').read_text()` returned `outside-secret`.
  - A second controlled experiment showed `os.replace()` succeeds on a junction and leaves the destination as a junction while the target continues to exist.

- Impact:
  - Violates the safe-relative-path and inventory invariant. A queue envelope can cause the proxy to read, hash, and make available files outside the queue root.
  - A worker can operate with an external directory as its job root, so worker-created outputs can be written into the target.
  - An invalid or completed junction job can publish an Answer path that is itself a link, so subscribers do not necessarily receive a regular immutable directory.
  - The trust model may reduce exposure if every queue writer already has local filesystem access, but the proxy's containment contract should not depend on that assumption.

- Contract:
  - `Docs/INVARIANTS.md` requires safe relative paths and inventories and subscriber-owned envelopes.
  - The existing validation policy rejects symlinks; junctions and other reparse points are the Windows equivalent and must not be treated as regular directories.

- Recommended remediation:
  - Add a shared `is_link_like(path)` helper that detects symlinks and reparse points. On Windows, use `os.lstat()` and the reparse-point attribute (or `Path.is_junction()` where available) and do not rely on `Path.is_symlink()` alone.
  - Reject link-like subscriber roots and job roots at scan/validation time, and reject link-like files/directories during inventory walking and path resolution.
  - Verify the resolved target stays under a canonical, non-link queue root; do not use a junction-resolved root as the boundary itself.
  - Apply the same helper in `_scan`, `_invalid`, `recover_interrupted`, managed-output handling, and `ai_worker.local_file()`.
  - If Python 3.10 support is retained, avoid a hard dependency on `Path.is_junction()` (introduced later); use `os.lstat` plus the Windows reparse tag/attribute with a capability check.
  - Consider rejecting all reparse-point job folders rather than trying to safely execute through them. A flat queue contract is simpler and safer.

- Acceptance evidence:
  - A junction at the subscriber or job root is reported invalid or skipped without reading or hashing target files.
  - No external path appears in `output_files`, no worker starts, and no Answer is published for a link-like job.
  - A symlink inside a job that escapes the job is still rejected.
  - Existing symlink-focused behavior remains unchanged, subject to Joe's test policy.

### CR-003 - Invalid-path handling inventories links before sanitizing and can write through a junction

- Priority: P2
- Confidence: high for the inventory order and path primitives; the exact `_invalid` write-through is inferred from the same primitives.
- Locations:
  - `file_proxy/engine.py:287-308` (`_invalid`), especially `output_files=_inventory_with_retry(path)` at line 302 and the only-symlink cleanup at lines 304-306.
  - `file_proxy/engine.py:383` (interrupted-result inventory before move).
  - `file_proxy/validation.py:15-27` (`build_file_inventory`).
  - `file_proxy/engine.py:251-258` (`_move_to_answer`).

- Trigger:
  - A job folder is invalid because its manifest is unreadable, its worker is unknown, or its inventory/path checks fail. If the folder is a symlink or junction, `_invalid()` builds an inventory before it attempts any cleanup.
  - For a normal symlink, the external files are hashed into the `INVALID` result before the link is unlinked and a real directory is created.
  - For a junction, `path.is_symlink()` is false, so `_invalid()` does not unlink it. It writes `proxy_result.json` through the junction into the external target and then `_move_to_answer()` moves the junction itself into `Answer`.
  - The interrupted-result path has the same inventory-before-containment pattern at line 383.

- Evidence:
  - `build_file_inventory()` walks `job_dir.rglob("*")` and follows links.
  - The controlled junction experiment confirmed inventory follows a junction and reads outside content.
  - The controlled `os.replace()` experiment confirmed replacing a junction leaves a junction at the destination and preserves the external target.

- Impact:
  - Names, sizes, and SHA-256 hashes of outside files can be published in an INVALID result, leaking information and imposing read cost on arbitrary targets.
  - A junction invalid job can cause the proxy to overwrite a `proxy_result.json` in the target directory and publish a link rather than a regular Answer directory.
  - This breaks the immutable published-envelope contract even when the original job is rejected.
  - The issue is related to CR-002 but has a distinct ordering cause: even if link detection is added, inventory must occur after containment.

- Recommended remediation:
  - Sanitize and contain the path before any inventory, result write, or move.
  - For a link-like invalid path, quarantine or rename the link itself without traversing it; do not write through it and do not invent an inventory from its target.
  - Inventory only paths proven to be regular, non-link directories/files under the canonical job root.
  - Apply the same rule to interrupted-result inventory and any other invalid/error publication path.
  - Decide explicitly whether invalid results should include any file inventory at all; if they do, make it link-safe.

- Acceptance evidence:
  - An invalid symlink or junction job publishes no outside file names or hashes.
  - `Answer/<sub>/<job>` is a regular directory, not a link.
  - The external target is unchanged and no `proxy_result.json` is written through the link.
  - Normal invalid jobs still publish a useful terminal INVALID result.

### CR-004 - Admission reservations leak when body normalization fails

- Priority: P2
- Confidence: high from static trace; the exception trigger is reproducible with standalone JSON/Python behavior.
- Locations:
  - `file_proxy/http_proxy.py:528-545` (`handle`).
  - `file_proxy/http_proxy.py:692-712` (`_read_body`).
  - `file_proxy/http_proxy.py:338-365` (`AdmissionController`).
  - `file_proxy/reasoning_policy.py:61-100` (`normalize`), especially line 90.

- Trigger:
  - A queued route body is fully read. `_read_body(..., admitted=True)` reserves each chunk through `admission.add()`, so `admission.bytes` includes the body size.
  - `handle` sets `buffered = len(body)` at line 532.
  - On an Ollama profile with an active reasoning policy, `reasoning_policies.normalize()` can raise after decoding JSON: for example, a valid JSON body containing an unpaired surrogate plus a recognized model alias and reasoning effort reaches `json.dumps(..., ensure_ascii=False, separators=(",", ":")).encode("utf-8")`, which raises `UnicodeEncodeError`.
  - The exception is caught by `except BaseException` at line 543, which calls `await self.admission.leave(0)`. That decrements the request count but releases zero buffered bytes.
  - `_read_body` has already returned success, so its own partial-byte release path is not active.
  - The leaked byte count remains until proxy restart. Repeated requests can consume `max_buffered_body_bytes`; subsequent queued body reads then return 503 even though the proxy is otherwise healthy.

- Evidence:
  - `leave(amount)` decrements `requests` and `bytes`; there is no separate request-only release.
  - The only failure-path cleanup in `handle` is `leave(0)`.
  - A normalization exception occurs after `buffered` is set.
  - The same shape appears if any post-read normalization step raises, not only the surrogate case.

- Related narrower ownership issue:
  - For settle routes, `handle` creates a lifecycle task and transfers admission ownership to it. If the handler is cancelled before the lifecycle coroutine starts and `job.dispatched` is false, it cancels the task. A task cancelled before its first execution may never run its `finally`, so the request and byte reservation may not be released. This is narrower than the normalization leak but has the same root cause: admission ownership is not explicitly transferred/released on every cancellation path.

- Impact:
  - A local or loopback client can exhaust buffered-body capacity with failed requests and force a proxy restart.
  - The listener remains available but queued routes become unusable until restart; this is an availability/DoS issue.
  - The default loopback listener reduces remote exposure, but the configured listener may be reachable by other local processes or by a configured non-loopback host.

- Recommended remediation:
  - Track the reservation independently and release exactly the reserved byte count on every failure path, for example `await self.admission.release_bytes(buffered)` followed by a request release, or provide a single `leave(request=True, bytes=buffered)` API with explicit ownership.
  - Transfer admission ownership to the lifecycle task only after it has definitely started, or make the task's cancellation-safe cleanup independent of coroutine startup (a wrapper/finally or an admission reservation object).
  - Treat normalization/encoding errors as HTTP 400 or a controlled failure; do not let a malformed unicode payload terminate the admission path without accounting.
  - Add observability for reserved/released bytes so a leak is visible without a restart.

- Acceptance evidence:
  - Sending the crafted failing request leaves `admission.requests` and `admission.bytes` at their pre-request values.
  - A subsequent queued request succeeds.
  - Cancelling a settle-route client immediately after enqueue does not reduce capacity permanently.
  - Any test additions require Joe's approval; a small admission-accounting test or an extension of an approved HTTP scenario would be appropriate.

### CR-005 - Declared Python 3.10 support is false for the CLI

- Priority: P2
- Confidence: high static; no Python 3.10 interpreter was available to execute the import.
- Locations:
  - `pyproject.toml:11`: `requires-python = ">=3.10"`.
  - `README.md:614` and `README.md:619`: "Python 3.10 or newer."
  - `file_proxy/reasoning_policy.py:4`: `from datetime import UTC, datetime`.
  - `file_proxy/http_proxy.py:25`: imports `reasoning_policy`.
  - `file_proxy/cli.py:10-11`: imports `engine` and `http_proxy`.
  - `Docs/DEVELOPMENT.md:8-9` acknowledges that Python 3.10 compatibility is unverified because of `datetime.UTC`.

- Trigger:
  - Install the package in a Python 3.10 environment permitted by `pyproject.toml`.
  - Run any CLI command, including `python -m file_proxy.cli --help`. `cli` imports `http_proxy`, which imports `reasoning_policy`, which imports `datetime.UTC`; the import fails before argument parsing.
  - `setup_venv.ps1`/`.bat` create an environment with the available Python and run the test suite, so a 3.10 machine would install successfully and then fail at import rather than at an explicit version check.

- Evidence:
  - `datetime.UTC` was introduced in Python 3.11.
  - The package metadata and README explicitly promise 3.10.
  - The development document already records the mismatch, so this is not a hidden behavior; it is an unresolved packaging/documentation contract.

- Impact:
  - A supported installation path is unusable; every CLI subcommand fails, including help and status.
  - The setup flow can create a broken deployment before revealing the version requirement.
  - Downstream integrations may pin Python 3.10 based on the package metadata.

- Recommended remediation:
  - Preferred: replace `datetime.UTC` with `timezone.utc` and verify the CLI/test suite on the declared minimum Python.
  - Alternative: raise `requires-python` to `>=3.11` and update README/DEVELOPMENT and any launcher/setup guidance. If this is chosen, document the decision as a packaging contract change.
  - If a Windows link-detection helper is added for CR-002, ensure its fallback works on the declared minimum rather than silently assuming `Path.is_junction()`.

- Acceptance evidence:
  - On the declared minimum Python, `python -c "import file_proxy.cli"` and `python -m file_proxy.cli --help` succeed, or the declared minimum is consistently raised everywhere.
  - Package metadata, README, DEVELOPMENT, and setup behavior agree.
  - No runtime test was added solely for this finding without approval.

### CR-006 - Restart reconciliation can terminalize a pending ComfyUI record without proving the previous backend ended

- Priority: P3
- Confidence: medium-high static; platform-dependent runtime behavior was not exercised.
- Locations:
  - `file_proxy/http_proxy.py:500-517` (`reconcile_records`).
  - `file_proxy/http_proxy.py:469-490` (`stop`, including `/interrupt` and cancellation grace).
  - `file_proxy/http_proxy.py:1361-1363` (reconciliation service selection).
  - `file_proxy/backend.py:383-425` (managed process stop/containment).
  - `Docs/COMPONENTS/HTTP_TRANSPORT.md` restart/outstanding-record section.
  - ADR 0005 and `Docs/INVARIANTS.md:28` for restart uncertainty.

- Trigger:
  - The proxy dies with a pending ComfyUI execution record. On restart, `reconcile_records()` polls history for only 0.25 seconds.
  - If no terminal history is visible, and managed ComfyUI is enabled, the record is unconditionally marked `failed` with `outcome="restart"` and the claim that the owned backend ended with the previous proxy.
  - The code does not verify that the previous owned process actually ended, nor whether an external endpoint is still executing the prompt. On Windows, job-object containment usually kills the managed child when the proxy handle closes; on POSIX there is no equivalent in this code path, and an external endpoint is outside the proxy's control.
  - If no ComfyUI listener is configured, the reconciliation service falls back to `services[0]`, which can be Ollama. Pending ComfyUI records are then probed against an unrelated upstream.

- Impact:
  - A recoverable result may be discarded if history is temporarily unavailable after restart.
  - An unresolved external backend can be marked terminal even though its prompt is still active; later local admission relies on other guards, and the record no longer reflects the uncertainty.
  - The fallback upstream choice can block or fail records for the wrong backend.
  - This is a correctness/recovery gap rather than a demonstrated Windows GPU overlap, so it is ranked P3.

- Contract:
  - Docs describe unresolved external ComfyUI work as blocking until intervention or verified stop.
  - ADR 0005 explicitly says restart alone is not evidence of a free GPU and that managed stop must be verified.
  - The current 0.25-second history check is much weaker than those statements.

- Recommended remediation:
  - Reconcile using backend-specific information only; do not fall back to a non-ComfyUI service for ComfyUI history.
  - Distinguish "history not yet visible" from "backend definitively ended"; use a bounded but explicit recovery window or preserve a blocked record for operator resolution.
  - Verify previous owned-process termination or block when termination cannot be proven.
  - Preserve the durable guard/fence until the record is terminal and the backend state is known.
  - Make graceful shutdown behavior explicit in docs if records are intentionally abandoned at shutdown.

- Acceptance evidence:
  - A restart with a pending ComfyUI record and no available history leaves local and ComfyUI admission blocked, or keeps the record in a clearly non-terminal blocked state.
  - After a verified stop or terminal history, the record becomes terminal and local work can resume.
  - No wrong-upstream request is made when no ComfyUI listener exists.
  - Any new scenario requires Joe's approval.

### CR-007 - Answer collisions raise an uncaught RuntimeError and can fail-stop scheduling

- Priority: P3
- Confidence: high static.
- Locations:
  - `file_proxy/engine.py:251-258` (`_move_to_answer`).
  - `file_proxy/engine.py:308`, `360`, `386`, `474`, `702` (call sites).
  - `file_proxy/engine.py:926-930`, `956-966`, and the `run()` error handling (only `OSError` is handled around selection/processing).
  - `file_proxy/cli.py` and `supervisor.py` propagate unexpected exceptions as runtime errors.

- Trigger:
  - `Answer/<subscriber>/<job>` already exists when a job reaches publication. This can happen after a contract violation (subscriber reuses a job ID), a stale Answer, manual repair, or a duplicate queue copy.
  - `_move_to_answer()` checks `destination.exists()` and raises `RuntimeError("answer already exists")`.
  - `recover_interrupted()`, `_publish_completed_running()`, `_invalid()`, and `_execute_file_job()` do not catch this specific error. Scheduler/CLI boundaries generally catch `OSError`, not `RuntimeError`.
  - A worker may already have executed and written its result before the collision is discovered, so the attempt cannot be safely retried by the proxy.

- Impact:
  - The scheduler or one-shot command can terminate instead of isolating the job; a supervisor may restart into the same condition, creating a crash loop.
  - The proxy cannot publish a result and cannot preserve the Running envelope cleanly. Operator intervention is required.
  - Because the original attempt already ran, automatic retry would risk duplicate effects; fail-stop is safer than replay but should be deliberate and observable.

- Contract:
  - The README requires subscribers to choose fresh retry IDs, so collision is a contract violation. The proxy should still fail closed without crashing the whole scheduler.

- Recommended remediation:
  - Catch Answer collisions at the publication boundary, log them with subscriber/job identity, and leave the Running envelope and result intact for operator action.
  - Do not move, delete, or overwrite the existing Answer.
  - Consider exposing a distinct status/error for duplicate publication and refusing to start new work for that subscriber/job until resolved.
  - Optionally check destination availability before executing a file job, while retaining the final atomic check.

- Acceptance evidence:
  - A duplicate Answer leaves the scheduler alive, the new job observable as failed/blocked, and the existing Answer unchanged.
  - Recovery does not repeatedly crash on the same collision.
  - No automatic retry of the already-executed attempt occurs.

### CR-008 - Local execution metadata grows without bound and can leave stale native results

- Priority: P3
- Confidence: high static; this is an operational/resource issue rather than an immediate correctness failure.
- Locations:
  - `file_proxy/engine.py:206-207` (`_execution_path`).
  - `file_proxy/engine.py:609-632` (execution journal creation and worker environment).
  - `file_proxy/engine.py:263-266` (`worker-outcomes`).
  - `file_proxy/engine.py:685` (native worker result).
  - `file_proxy/engine.py:722` (native submission sidecar).
  - `file_proxy/ai_worker.py:34-49` and `worker_host.py` (sidecar/journal writes).
  - `file_proxy/engine.py:251-258` removes only the local outcome after Answer publication.

- Trigger:
  - Every local file job uses `executions/<subscriber>/<job>.json` as its process witness. Native workers also write `.result.json` and ComfyUI workers write `.submission.json`.
  - After the job moves to `Answer`, the code removes `worker-outcomes/<subscriber>/<job>.json` only. The execution journal and native result/submission sidecars are not removed by any inspected path.
  - Over time the runtime directory retains one or more files per completed job. A queue that reuses a job ID can read old recovery metadata, and an interrupted/retried job can inherit a stale native result if the new worker does not overwrite it.

- Impact:
  - Unbounded local disk consumption, especially for deployments processing many small jobs.
  - Stale `.result.json` data can influence `recover_interrupted()`/`_restore_outcome()` for a later attempt if the proxy stops during that attempt.
  - Stale `.submission.json` data can cause unnecessary guard creation or recovery work.
  - The issue is worse on fixed local storage that is intended to be independent of the removable queue volume.

- Recommended remediation:
  - Define a retention policy for execution journals, native results, and submission sidecars; clean them after successful Answer publication, or retain only bounded recovery metadata with an explicit TTL.
  - Do not delete metadata that is still needed to classify a live attempt; use the existing phase/ownership records to decide.
  - Include a generation/attempt token or creation time in recovery records so a reused job ID cannot consume an earlier attempt's native result.
  - Document the retention behavior in the data model if it becomes configurable.

- Acceptance evidence:
  - After a representative batch of successful jobs, execution metadata is bounded by the retention policy rather than growing per job.
  - A new attempt with a reused job ID cannot read an older attempt's native result or submission state.
  - Crash recovery still works for in-flight attempts.

### CR-009 - Cloud model-catalog refresh thread can die on a queue unlink failure

- Priority: P3
- Confidence: high static.
- Locations:
  - `file_proxy/engine.py:807-829` (`refresh_catalog`).
  - `file_proxy/engine.py:814-822` (refresh and request unlink).

- Trigger:
  - `catalog.refresh()` may succeed or fail; the thread catches `OSError` around `catalog.refresh()`.
  - The subsequent `request.unlink(missing_ok=True)` is outside the try block. If the queue disappears or the control file cannot be unlinked due to sharing/permissions, `unlink` raises and terminates the daemon thread.
  - No watchdog restarts the catalog thread; the model catalog remains stale until the proxy restarts.

- Impact:
  - Model selectors and status may show stale models indefinitely.
  - A queue outage specifically increases the chance of the failure, so the recovery path degrades catalog freshness.
  - The thread is also not joined during context shutdown; this is secondary because it is a daemon, but it complicates deterministic shutdown tests.

- Recommended remediation:
  - Put the unlink inside the same guarded region and log failures without terminating the loop.
  - Treat a missing control file as a no-op; treat an unlink failure as retryable.
  - Join the catalog thread in the `finally` path if the surrounding shutdown contract permits it, or explicitly document daemon behavior.
  - Do not let auxiliary catalog maintenance affect core scheduling readiness.

- Acceptance evidence:
  - A forced unlink failure leaves the refresh loop alive and it retries on the next interval.
  - Queue disappearance does not permanently stop model refresh.
  - Shutdown does not leave a non-daemon thread or unresolved runtime resource.

### CR-010 - A duplicate upstream prompt ID can archive artifacts against an inconsistent record

- Priority: P3
- Confidence: high static; requires an upstream behavior that returns an ID already tracked by another request.
- Locations:
  - `file_proxy/http_proxy.py:847-858` (returned ID comparison and `reconcile_prompt_id`).
  - `file_proxy/http_proxy.py:911-922` (generic upstream exception reconciliation).
  - `file_proxy/http_proxy.py:1001-1095` (`_archive_completion`, artifact storage).
  - `file_proxy/store.py:74-79` (`reconcile_prompt_id` unique constraint).
  - `file_proxy/store.py:122-133` (artifact keyed by prompt ID).

- Trigger:
  - ComfyUI returns a prompt ID that is already present in the proxy's unique `prompt_id` record.
  - The code assigns `known_id = returned_id` before calling `store.reconcile_prompt_id()`.
  - `reconcile_prompt_id()` raises `FileExistsError`, which is converted to `web.HTTPConflict`.
  - The generic upstream exception handler treats the conflict as an upstream failure, then polls history for the returned ID and may archive it. The new execution row still has the preassigned prompt ID because the update failed, while artifacts are inserted under the returned prompt ID.
  - History lookup by the new request's prompt ID will not find that archived history, and artifacts may be attributed to the existing execution.

- Impact:
  - Inconsistent cache/history rows and potentially cross-request artifact attribution.
  - A client can receive a successful response while the proxy cannot serve the corresponding cached history under the request's recorded prompt ID.
  - The existing execution's artifact set may be overwritten or duplicated depending on filenames and cache paths.

- Recommended remediation:
  - Treat a returned duplicate prompt ID as a terminal rejected/error condition for the new request; do not poll or archive against the returned ID.
  - Validate the returned ID before assigning it to `known_id`, and keep the execution row and lifecycle ID consistent.
  - If reconciliation is supported, make the execution update and artifact insertion part of one consistent transaction or state transition.
  - Log both request IDs and refuse to overwrite another execution's artifacts.

- Acceptance evidence:
  - A simulated duplicate returned ID leaves both records consistent, with no cross-request artifacts and no cached history for the wrong prompt.
  - The client receives an explicit conflict/error rather than a success with unreachable cache data.
  - Existing prompt-ID uniqueness behavior is preserved.

### CR-011 - Corrupt local JSON can crash startup, recovery, or a worker after settlement

- Priority: P3
- Confidence: high static; corrupt files are plausible after disk interruption even though normal writes are atomic.
- Locations:
  - `file_proxy/storage_health.py:45-48` parses the storage-health journal with only `FileNotFoundError` handled.
  - `file_proxy/engine.py:743` parses the ComfyUI guard without a corruption handler.
  - `file_proxy/engine.py:724` parses a native submission sidecar.
  - `file_proxy/engine.py:321`, `331`, `342`, `357` parse recovery journals/outcomes.
  - `file_proxy/ai_worker.py:46` parses a guard after a successful native submission.
  - `file_proxy/diagnostics.py:53-62` can propagate an `OSError` from cleanup during construction.

- Trigger:
  - A truncated or invalid `.json` guard, submission sidecar, execution witness, worker outcome, or storage-health journal is present.
  - `StorageHealth.__init__()` raises `json.JSONDecodeError`, so every CLI/Proxy construction may fail before the proxy can enter degraded mode.
  - `_clear_unsubmitted_guard()` raises in `process_once` before it can decide whether local admission is safe. The exception is not an `OSError`, so the scheduler's filesystem-degradation handling does not catch it.
  - `recover_interrupted()` can raise on a corrupt witness or native result. `_queue_ready()` catches only `OSError`.
  - `ai_worker.save_submission()` can fail after the native prompt has settled if the guard is corrupt, turning a successful render into a worker error.

- Impact:
  - The proxy can fail to start or can crash a scheduler iteration instead of failing closed and preserving the bad record.
  - A corrupt guard is especially sensitive because it is the safety fence; the proxy should treat unreadable guard state as blocking, not as absent and not as a startup crash.
  - A corrupt native result can prevent publication of otherwise valid work.

- Recommended remediation:
  - Centralize local JSON reads with a recovery policy: catch `OSError`, `UnicodeError`, and `JSONDecodeError`; log the path; quarantine/rename corrupt state when safe; otherwise fail closed with a clear blocked reason.
  - For the ComfyUI guard specifically, unreadable state must block local execution and require operator intervention; never treat parse failure as "no guard."
  - Decide whether corrupt execution witnesses/results should block the specific job or be ignored with a recorded failure; do not crash the whole scheduler.
  - Make the storage-health journal optional/best-effort on corruption, while preserving the ability to report degraded storage.

- Acceptance evidence:
  - A corrupt guard prevents local processing and reports a clear blocked/error state without crashing the CLI.
  - A corrupt execution witness is isolated to the affected job.
  - A corrupt sidecar after settle does not prevent the worker from recording its terminal result.
  - No runtime test is added without Joe's approval.

## 4. Maintainability opportunities

- `StorageHealth.check_error()` has no callers. Runtime SQLite errors from `ExecutionStore` are not classified through the storage-health mechanism, and `_archive_completion()` catches filesystem/JSON errors but not `sqlite3.Error`. A future packet should define the intended boundary between queue storage health and local metadata health rather than leaving this as dead code.
- The admission lifecycle lacks a single owner/lease abstraction. Request count, byte count, queue slot, and lifecycle task cleanup are spread across `handle`, `_read_body`, `_run_prompt_lifecycle`, `stop`, and scheduler code. A small reservation object would make exactly-once release easier to review.
- Guard handling is duplicated across `_clear_unsubmitted_guard`, `_preserve_unsettled_backend`, `recover_interrupted`, and worker submission. A typed guard/sidecar model with explicit phases and one parse/validate path would reduce the risk of inconsistent behavior.
- `once` duplicates part of the scheduler path but omits guard/orphan admission checks. Either make `once` a thin call into a shared guarded selection path, or document and enforce its narrower capabilities without allowing unsafe local work.
- The scheduler's backend/state error handling has several overlapping `OSError`, `RuntimeError`, `BackendUnavailable`, and `web.HTTPException` branches. A small error classification layer would clarify which errors pause the queue, which block local admission, and which terminalize a record.
- `BackendManager.released()` can set `ready` or `stopped` while leaving `blocked_reason` populated. Status and dashboard code then have to reconcile two fields. Clear the reason on a legitimate transition or model state and reason as one value.
- `_cancel_and_reconcile()` can raise `BackendUnavailable` from inside the upstream exception handler, leaving a record in `cancelling` after the generic outer handler swallows the error. Recovery should have an explicit terminal/blocked transition for this case.
- `validate_structure()` implements a deliberate subset of JSON Schema. That is acceptable if documented as a subset; ensure subscriber docs never imply full schema support.
- `setup_venv.ps1` line 13 uses `pip install --editable "$PSScriptRoot[dev]"`. PowerShell interpolation of the path followed by an extras expression is easy to misread and was not verified on this host. Prefer an explicit joined path/argument form so setup behavior is obvious.
- The suite has a strong representative E2E baseline, but the native guard recovery branch, junction containment, admission exception cleanup, corrupt local state, and duplicate Answer collision are not directly covered. ADR 0005 explicitly records the native guard gap. Any test additions require Joe's explicit approval; otherwise keep the recommendations as manual acceptance checks.
- There is no configured linter, type checker, or CI workflow in this checkout (`DEVELOPMENT.md` says so and no `.github`/CI files were found). That is a project decision, but it increases the value of a small set of deterministic invariant checks.

## 5. Documentation discrepancies

| Document | Current statement | Actual behavior / evidence | Recommended correction |
| --- | --- | --- | --- |
| `Docs/DEVELOPMENT.md:30` and `:37` | The replacement suite is represented by five isolated scenarios; the baseline table records five passing. | `tests/test_e2e.py` defines six isolated scenarios; `Docs/DEVELOPMENT.md:191` and `:197` also say six passed. | Make the baseline consistently say six isolated scenarios and one deselected live scenario. |
| `Docs/DEVELOPMENT.md:89` | The standard launchers default the queue to `%USERPROFILE%\Dropbox\AI_Queue`. | `run_file_proxy.ps1:15` and `run_file_proxy.bat:15` default to `E:\Zet\AI_Queue`. | State the actual launcher default, or change the launchers to match the documented default. |
| `README.md:625-627` | The launcher queue is implied to be `%USERPROFILE%\Dropbox\AI_Queue`; set `AI_QUEUE_ROOT` if it is elsewhere. | The shipped launchers use `E:\Zet\AI_Queue`. | Align the deployment narrative with the scripts or make the scripts read a documented default. |
| `README.md:614`, `README.md:619`, `pyproject.toml:11` | Python 3.10 or newer is required/supported. | `file_proxy/reasoning_policy.py:4` imports `datetime.UTC`; `cli.py` imports `http_proxy`, which imports `reasoning_policy`, so CLI import fails on 3.10. `Docs/DEVELOPMENT.md:8-9` acknowledges the mismatch. | Either fix the import for 3.10 or raise the minimum version consistently in all packaging and deployment docs. |
| `Docs/DEVELOPMENT.md:8-9` | Python 3.10 compatibility is unverified because of `datetime.UTC`. | This is internally honest, but it conflicts with the package/README promise; the setup path can still install on 3.10. | Treat this as an unresolved contract, not only a note. |
| `Docs/DEVELOPMENT.md:20-22` | There is no configured linter, type checker, or CI workflow. | No `.github`, GitLab, Azure, tox/nox, or equivalent configuration was found. | Accurate; no change needed unless CI is added. |
| `Docs/DEVELOPMENT.md:191-198` and ADR 0005 | Six isolated scenarios passed; the native guard branch is not directly covered. | The suite contains six isolated tests and one live test; the guard-recovery branch is not directly exercised. | Keep the coverage caveat; do not imply the safety fence has direct automated coverage. |
| `runtime-config.example.json` and ADR 0003 | No proactive ComfyUI launch; retain an already-running managed ComfyUI while idle; legacy `idle_shutdown_seconds` is ignored and omitted. | The example config matches: managed ComfyUI is disabled by default and no `idle_shutdown_seconds` key is present. | No discrepancy found. |

A repository-wide relative-link check covered 21 Markdown files under `Docs/`, `README.md`, `AGENTS.md`, and this report after it was written; no missing targets were found.

## 6. Validation results and coverage limits

### Commands and observations

| Check | Result |
| --- | --- |
| `.\.venv\Scripts\python.exe -m pytest -q` | Could not run: the `.venv` directory is absent and PowerShell reported `CommandNotFoundException`. |
| System `python --version` | Python 3.14.6 is available. |
| `python -c "import aiohttp"` equivalent availability check | `ModuleNotFoundError: No module named 'aiohttp'`; the system environment does not provide the application runtime dependencies. |
| `python -m compileall -q file_proxy tests` | Passed. Generated `__pycache__` directories were removed afterward. |
| `git status --short` after cleanup | Before this report, no output (repository clean). After writing, the only reported path is the untracked `Docs/CodeReview.md`. |
| `git diff --check` | No output for tracked changes. Because the report is untracked, its whitespace was checked separately: 0 trailing-whitespace or tab findings. |
| Markdown relative-link check | 21 files checked including this report; 0 missing links. |
| Windows junction validation experiment | Confirmed `Path.is_symlink()` false for a junction, `Path.is_junction()` true, `validate_job_folder()` accepted the junction, inventory read the external target, and the external secret was readable through the job path. |
| Windows junction replacement experiment | Confirmed `os.replace()` moved a junction and left a junction at the destination with the target intact. |
| Live backend scenario | Not run and intentionally not enabled. |
| Python 3.10 import check | Not run; no 3.10 interpreter is installed. The incompatibility is established statically from `datetime.UTC`. |

### What is verified vs inferred

- Verified in this environment: repository state, compilation, documentation links, Windows junction acceptance/inventory behavior, and `os.replace()` behavior on a junction.
- Verified statically with high confidence: guard-check ordering, admission leak path, declared Python version mismatch, restart reconciliation branches, publication collision handling, and local metadata cleanup gaps.
- Not verified at runtime: actual native ComfyUI guard recovery, live provider behavior, external backend shutdown, real SQLite failure handling, process containment on POSIX, the full pytest suite, and the behavior of the live backend scenario.
- This review did not install dependencies, enable live backends, modify registrations, or run `run`, `once`, or `tui` against a real queue. Consequently, no claim is made that the maintenance suite currently passes on this host.

### Coverage limits

- The maintained isolated suite does not directly exercise native ComfyUI guard recovery, junction/reparse containment, admission normalization failures, corrupt local JSON, Answer collisions, or duplicate upstream prompt IDs.
- The live backend scenario is opt-in and must remain deselected by default; it was not used.
- The documented historical baseline (six isolated scenarios passed; live deselected) is evidence from prior runs, not from this review.
- No new tests or test branches were added because the current working agreement requires Joe's explicit approval for test additions.

## 7. Planning handoff

The following packets are intentionally small and ordered. They are planning guidance, not authorization to edit code. Any test addition or expansion still requires Joe's approval; implementation should use the smallest relevant existing scenario or a manual acceptance check where possible.

1. **Packet A - Enforce native guard atomicity (CR-001)**
   - Scope: `file_proxy/engine.py`; possibly shared helper and focused docs wording.
   - Work: create one guard predicate; use it in `once`, `process_once`, scheduler selection, and immediately before local worker launch; ensure recovery cannot create a guard after the final check.
   - Done when: ambiguous guard blocks both one-shot and continuous local execution; PREPARED clearing remains safe; managed verified-stop behavior remains intact.
   - Validation: targeted existing scheduler/guard scenario or manual two-job startup check; no unapproved test expansion.

2. **Packet B - Contain Windows reparse points before inventory and publication (CR-002, CR-003)**
   - Scope: `file_proxy/validation.py`, `file_proxy/engine.py`, `file_proxy/ai_worker.py`, and the managed-output check in `http_proxy.py`.
   - Work: shared link-like detector with a Python-version-compatible fallback; reject root/nested junctions and reparse points; inventory only after containment; ensure `_invalid` cannot write through or move a link.
   - Done when: junction jobs are rejected without external reads/hashes; no Answer link is published; existing symlink behavior remains.
   - Validation: controlled junction check and the existing validation/symlink scenario if approved; otherwise manual acceptance.

3. **Packet C - Make admission reservations exactly-once (CR-004)**
   - Scope: `file_proxy/http_proxy.py` admission path and normalization failure handling.
   - Work: explicit reservation owner; release bytes on all exception/cancellation paths; safe settle-route task cancellation; controlled 400 for unencodable normalization payloads.
   - Done when: crafted normalization failure returns capacity to baseline; immediate client cancellation cannot leak capacity; status remains available.
   - Validation: injected failure check or an approved extension of the existing HTTP scenario.

4. **Packet D - Align Python support and packaging (CR-005)**
   - Scope: `file_proxy/reasoning_policy.py`, `pyproject.toml`, `README.md`, `Docs/DEVELOPMENT.md`, setup scripts.
   - Work: either replace `datetime.UTC` for 3.10 or raise the minimum to 3.11; make package metadata, setup flow, docs, and any link-detection fallback agree.
   - Done when: the declared minimum can import `file_proxy.cli` and show `--help`, or the minimum is explicitly raised everywhere.
   - Validation: import/help on the supported interpreter plus a packaging metadata check.

5. **Packet E - Strengthen restart/recovery state transitions (CR-006, CR-011)**
   - Scope: `file_proxy/http_proxy.py`, `file_proxy/engine.py`, `file_proxy/storage_health.py`, `file_proxy/ai_worker.py`.
   - Work: backend-specific reconciliation; no wrong-upstream fallback; explicit blocked vs terminal states; corrupt local JSON policies; unreadable guard fails closed.
   - Done when: restart uncertainty remains blocking or is resolved only after verification; corrupt guard does not crash the CLI; corrupt per-job state does not terminate the scheduler.
   - Validation: focused manual recovery checks and approved existing restart scenarios.

6. **Packet F - Close low-severity operational gaps (CR-007, CR-008, CR-009, CR-010)**
   - Scope: `file_proxy/engine.py`, `file_proxy/http_proxy.py`, `file_proxy/store.py`, `file_proxy/backend.py`.
   - Work: Answer-collision isolation; bounded execution metadata; catalog thread error containment; duplicate returned-ID rejection without cross-record artifacts; status/reason cleanup.
   - Done when: scheduling survives collisions, local metadata is bounded, catalog refresh recovers from unlink failure, and prompt-ID conflicts cannot cross-attribute artifacts.
   - Validation: focused manual checks; any new test requires approval.

7. **Packet G - Documentation synchronization and final validation**
   - Scope: `Docs/DEVELOPMENT.md`, `README.md`, `Docs/INVARIANTS.md` if semantics change, component docs, and this review's referenced discrepancies.
   - Work: fix test counts, launcher defaults, Python version statement, and any newly clarified recovery semantics; run the smallest relevant checks and `git diff --check`.
   - Done when: docs match implementation and the active plan records observed results, deviations, and unverified claims.
   - Note: documentation maintenance is part of completion; do not invent historical rationale.

### Sequencing and guardrails

- Do Packet A first; it is the only P1 and it changes the safety boundary.
- Packet B can follow immediately because it also affects the meaning of a "job folder" and should be settled before any broader validation work.
- Packet C and D are independent and can be scheduled around A/B; D may constrain the fallback implementation used in B.
- Packet E should follow A so recovery behavior is reviewed against the corrected admission fence.
- Packet F is lower priority and should not delay the safety and containment fixes.
- Packet G is last and must record actual command outcomes. Do not claim a passing suite unless it was run successfully in the same environment.
- Keep one agent, preserve unrelated changes, and do not add production dependencies. Use the existing Python patterns: dataclasses for contracts, `pathlib`, `argparse`, `aiohttp`/`asyncio`, and pytest with `asyncio.run` scenarios.

## 8. Open questions

1. Is queue content always produced by a trusted subscriber process with unrestricted local filesystem access, or can a synced/replicated queue contain untrusted links? The severity of CR-002/CR-003 depends on this trust model, but the proxy's containment invariant should not assume the more permissive answer.
2. Should `once` be permitted to process local work at all when a native ComfyUI guard exists, or should it only clear PREPARED state and refuse ambiguous state? The current implementation effectively chooses "process anyway," which conflicts with the documented fence.
3. Is Python 3.10 support intentional, or should the project require Python 3.11+? This choice affects packaging, setup, and the implementation of Windows link detection.
4. On graceful shutdown, is it intentional that an external, unresolved ComfyUI record can become terminal `shutdown` while an in-flight render may continue upstream? If so, the distinction from crash/restart uncertainty should be documented explicitly.
5. Should invalid results contain any inventory of files that failed validation? If yes, what link-safe behavior is acceptable? If not, removing that inventory simplifies containment and avoids information exposure.
6. What retention policy is desired for local execution journals, native results, and submission sidecars: bounded recovery metadata, forensic retention, or cleanup after every publication?
7. Should a corrupt local guard fail closed forever until manual intervention, or should there be an explicit recovery command that quarantines it with an audit record? The current code has no consistent policy.
8. Should auxiliary services such as the cloud model-catalog refresher be allowed to fail independently without affecting readiness, and what telemetry should expose that failure?
