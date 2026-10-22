# System contracts

These contracts are supported by implementation/tests or the existing subscriber
integration agreement. Operational requirements are labeled where enforcement is
outside the proxy. A test reference is a verification target, not a claim that
every possible case has coverage; see [DEVELOPMENT.md](DEVELOPMENT.md) for results.

## File and application ownership

| Rule and why | Implementation / affected consumers | Verify |
| --- | --- | --- |
| Published asks are immutable; workers preserve `job.json` and declared inputs. This prevents partial-input execution and identity drift. Application owns publication; proxy owns queue moves. | [validation.py](../file_proxy/validation.py): `validate_job_folder`; [engine.py](../file_proxy/engine.py): `_process_file_job` validates again after worker. Affects publishers/workers/harvesters. Immutability itself is an integration contract, not OS write protection. | [File task scenario](../tests/test_e2e.py) checks inventory and unsafe output handling; publisher immutability itself is not enforced by the proxy. |
| Inventoried bytes must match before execution. Missing/mismatched manifest or declared files wait within sync grace; invalid contracts become terminal answers. This accommodates eventual sync. | `validate_file_inventory`, `Proxy._scan`; affects sync timing and error classification. Malformed non-manifest payload JSON is invalid, not deferred. | [File task scenario](../tests/test_e2e.py) verifies delayed input completion, inventory publication and unsafe worker output. Grace expiry is not covered. |
| Identity must match directory and paths must remain inside the envelope under Windows/POSIX checks. Link-like roots and nested reparse points are rejected before inventory. API route strings have explicit exemptions. This keeps queue references portable and contained. | `models` validators; `validate_relative_reference`, `_path_values`, `validation.is_link_like`; affects schemas, worker JSON, symlinks and Windows junctions. It is not a general sandbox for trusted worker code. | File task scenario checks unsafe worker-output handling. Reparse-point and API-route exemptions are not separate scenarios. |
| Execute only registered local argv, append `--job-dir`, use `shell=False`. Queue writers do not choose executable code. | `Registry.load`, `WorkerRegistration`, `Proxy._process_file_job`; affects all subscriber commands and cwd/environment. | File task scenario launches a registered temporary subprocess. Registry expansion and launch-failure branches are not separate scenarios. |
| Single attempt, no automatic worker replay. An interrupted unknown outcome becomes `PROXY_INTERRUPTED`; known journal/result is republished. Avoid duplicate external effects. | `recover_interrupted`, `_publish_completed_running`, `_persist_local_outcome`; affects restart, publication failure, subscriber retries. | Dashboard scenario verifies interrupted recovery without replay. Worker launch/timeout and journal-publication failure branches are not separate scenarios. |
| Answer results and full inventory must be durably harvested before subscriber retention; retry uses a fresh attempt ID. This is subscriber-owned, not an implemented harvester. | [README contract](../README.md), `ProxyResult`, `_move_to_answer` preserves an existing answer and leaves a collision blocked for operator action. Affects subscriber idempotency and retention. | Integrating app tests must cover duplicate harvest, partial answers, new-ID retry; no subscriber tests here. |

## Scheduling and process safety

| Rule and why | Implementation / affected consumers | Verify |
| --- | --- | --- |
| One assigned proxy per queue; coordinate local instances through the same runtime directory. Local lock is not distributed. Prevent concurrent queue ownership and GPU oversubscription. | `Proxy.lock`, `RuntimeLock` in engine; affects launch configuration, separate runtime dirs/machines. Operational enforcement remains necessary. | [Dashboard lifecycle scenario](../tests/test_e2e.py) verifies runtime lock acquisition/release. Multi-process lock contention is not covered. |
| Active expensive work is not preempted. HTTP waits for `finished`; async file-worker shutdown awaits shielded worker. Prevent overlap and half-finished output. | `Proxy.run_scheduler`; [scheduler.py](../file_proxy/scheduler.py): futures and `finish`; supervisor stop. Affects HTTP handlers/lifecycle tasks/TUI. | [Shared scheduler scenario](../tests/test_e2e.py) verifies HTTP/file ordering and streaming; [ComfyUI lifecycle scenario](../tests/test_e2e.py) verifies keyed-history settlement; dashboard scenario verifies safe worker stop. |
| Affinity must not leapfrog an earlier backend block; file affinity stays within highest eligible priority. HTTP cannot overtake the selected older file job when both backends are known and different. Prevent model reuse from starving backend switches. | `Proxy._choose_resource_job/run_scheduler`, `HttpQueue._candidate`; affects ordering, trusted backend declarations, resources. | Shared scheduler scenario checks non-preemption. Priority and cross-backend affinity combinations are not separate scenarios. |
| HTTP control mutations are affinity barriers; oldest override and streak bound apply only within the eligible backend prefix. Prevent reordering past evidence-erasing controls. | `HttpProxyService.handle` sets barriers; `HttpQueue._candidate`; affects queue/history POSTs and affinity. | Inspect candidate prefix/barrier selection; direct barrier/oldest-override tests are currently absent. |
| Never adopt or terminate manual ComfyUI. Stop only owned processes, confirm endpoint closure, unload/verify Ollama before managed ComfyUI startup. Protect user processes and GPU residency. | [backend.py](../file_proxy/backend.py): `ensure_comfyui`, `stop_comfyui`, `_unload_ollama`; affects file/HTTP backends, Windows Job Object. | Controlled ComfyUI lifecycle checks disposable-process ownership; opt-in live scenario checks installed runtime handoff. Manual-listener refusal and real backend behavior remain unverified until live validation runs. |
| An external unresolved ComfyUI prompt retains the slot until intervention/shutdown; restart uncertainty blocks new execution. Avoid unsafe inference overlap after ambiguous cancellation. | `HttpProxyService._cancel_and_reconcile/reconcile_records`, `BackendManager.block`; affects downstream timeout, shutdown, restart. | Inspect deadline/cancellation branches; full external-unresolved integration test is a gap. |

## HTTP, persistence, and prompts

| Rule and why | Implementation / affected consumers | Verify |
| --- | --- | --- |
| A ComfyUI prompt has a validated canonical-form UUID and durable uniqueness record before dispatch; the actual returned valid ID can be reconciled. Track one execution rather than unrelated history. | `_prepare_prompt_id`, `_extract_prompt_id`, `ExecutionStore.create/reconcile_prompt_id`; affects prompt clients, keyed cache, history. Reuse protection lasts while metadata is retained. | [ComfyUI lifecycle scenario](../tests/test_e2e.py) verifies held slot, keyed settlement and cached history after restart. Prompt reuse rejection is not covered. |
| Prompt POST response completion does not release the slot; the owned lifecycle task waits for keyed history or explicit reconciliation. Client disconnect after dispatch does not discard it. | `handle`, `_run_prompt_lifecycle`, `_wait_for_prompt_completion`; affects scheduler and client cancellation. Parsed keyed dict is considered terminal; success is checked separately. | ComfyUI lifecycle scenario checks the slot through successful keyed history. Terminal-error and client-disconnect branches are not separate scenarios. |
| Cache artifacts before exposing archived history; terminal errors are not successful renders. Managed original-output deletion requires successful history, owned managed backend, nonempty cache file, and contained output path. | `_archive_completion`, `_archive_artifact`, `_remove_managed_output_duplicates`, `_managed_output_path`; affects history/view clients, output storage/retention. | ComfyUI lifecycle scenario verifies cached artifact retrieval after shutdown and restart. Error and eviction handling are not separate scenarios. |
| Undispatched HTTP bodies stay memory-only and are never replayed after restart. Durable metadata reconciles outcomes, not requests. Avoid duplicate accepted effects and sensitive payload persistence. | `HttpProxyService`, [store.py](../file_proxy/store.py), `run_http_proxies`; affects overload/cancellation/restart. | Store schema and lifecycle tests; inspect persistence changes for accidental body storage. |
| HTTP diagnostics at current call sites contain no bodies/headers. Captured worker/backend logs have separate contracts. Keep transport observability from copying payloads. | [diagnostics.py](../file_proxy/diagnostics.py), `_write_diagnostic` callers; affects logs/privacy. Logger accepts arbitrary details, so review new call sites. | Shared scheduler scenario checks the request identifier in streamed responses. Diagnostic field privacy and retention are not separately exercised. |
| No proxy-wide system prompt or task inference from alias/worker. Only documented alias effort normalization on `POST /v1/chat/completions`; native routes and unknown inputs stay unchanged. Invalid policy reload keeps last good policy. | [reasoning_policy.py](../file_proxy/reasoning_policy.py), [retained prompt ownership note](../Docs-Joe/System_Prompt_Review.md); affects subscriber prompts, alias metadata and HTTP compatibility. | [Policy-through-HTTP scenario](../tests/test_e2e.py) verifies mapped effort, unchanged messages, native route and malformed reload retention. |

Changes to any contract need an explicit impact analysis covering the listed
consumers and validation gaps; record compatibility changes in data/component
docs and a durable decision when appropriate. Do not silently strengthen these
claims into guarantees the implementation does not provide.

## Native cloud/local boundary (2026-10-16)
Trusted worker registration, never payload content, determines cloud admission.
Codex work must not prepare/release local backends or update local affinity. Local
operations retain one non-preemptive slot; cloud capacity defaults to two. Recovery
runs before admission and never replays interrupted attempts. Unresolved native
ComfyUI submissions durably block local execution, not cloud execution. An
unreadable guard also blocks local execution and requires operator recovery.
Managed recovery may archive a known ambiguous guard only after stopping its
owned backend and verifying the endpoint is gone; external ambiguity remains blocked. Terminal attempts are
never replayed or rewritten by guard recovery. Native
provider code imports no subscriber application and does not rewrite task prompts.

## Queue availability and witnesses
Unavailable storage is not an invalid job or authorization to recreate a vanished envelope. Reconnect recovery first respects live worker witnesses and known outcomes. Reserved launches are fenced locally before abandoned execution is classified. Retryable interruption/storage outcomes allow a subscriber to create a new attempt; they never authorize proxy replay. An unsubmitted native PREPARED guard is distinct from ambiguous dispatch and can be cleared without backend shutdown. See [ADR 0006](DECISIONS/0006-storage-outage-recovery.md).
