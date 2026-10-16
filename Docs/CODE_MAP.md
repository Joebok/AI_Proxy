# Behavior-to-code map

Use each row as a search starting point. Paths link to authoritative modules;
symbols identify actual entry points. Read impacted consumers/tests before editing.
Detailed contracts belong to the linked component or data-model document.

| Capability / change | Implementation and principal symbols | Callers, consequences, tests |
| --- | --- | --- |
| CLI, configuration, operating modes | [cli.py](../file_proxy/cli.py): `parser`, `_add_run_arguments`, `_http_configs`, `main`; [runtime.py](../file_proxy/runtime.py): `RuntimeConfig.load/resolved/validate`; [launch PS1](../run_file_proxy.ps1), [BAT](../run_file_proxy.bat) | Console entry point and launchers; impacts headless/TUI parity, backend settings, queue/runtime paths. [File task scenario](../tests/test_e2e.py): CLI publication and subprocess execution; [dashboard scenario](../tests/test_e2e.py): supervisor recovery/restart. |
| Manifest and envelope validation | [models.py](../file_proxy/models.py): `JobManifest.from_dict`, identifier/resource/priority validators; [validation.py](../file_proxy/validation.py): `validate_job_folder`, `validate_file_inventory`, `validate_relative_reference`, `_path_values` | File scans, post-worker validation, status; affects subscribers, sync deferral and worker outputs. [File task scenario](../tests/test_e2e.py): delayed input completion, successful result inventory and unsafe worker output. |
| Trusted worker registration | [registry.py](../file_proxy/registry.py): `Registry.load/_expand/worker/validate_subscriber`; `WorkerRegistration.from_dict`; [registries](../registries) | CLI loads every JSON registry at startup; engine resolves commands/backends. Environment/cwd failures can block all CLI commands. Reserved `AI_PROXY_PYTHON`/`AI_PROXY_PROJECT_ROOT` expansions bind native workers to the active runtime. File task scenario executes a temporary trusted command; registry expansion is not a separate scenario. |
| File lifecycle, subprocesses, recovery | [engine.py](../file_proxy/engine.py): `Proxy._scan/_select_file_job/_process_file_job`, `_invalid`, `_persist_local_outcome`, `recover_interrupted`, `_publish_completed_running`, `_move_to_answer` | `once`, synchronous run, async scheduler, supervisor; affects immutable inputs, output inventories, retry semantics and durable outcomes. File task scenario checks success/failure/invalid outcomes; dashboard scenario checks interrupted recovery and safe stop. [File transport](COMPONENTS/FILE_TRANSPORT.md). |
| Singleton and queue status | `RuntimeLock`, `Proxy.lock/ensure_layout/status` in engine; `HttpProxyService._special_route` | Runtime startup/restart, CLI status and dashboard; local lock scope, filesystem degradation, diagnostics and display state. Dashboard scenario checks isolated lock lifecycle; foreign-host lock and detailed status fields are not separate scenarios. |
| File ordering and worker resource hints | `Proxy._choose_resource_job/_file_job_backend/_record_resource_key/_next_resource_hint` in engine | File selector and async scheduler; impacts priority, affinity bounds, backend transitions and `AI_PROXY_*` worker environment. Priority/affinity combinations are not separate scenarios. [Scheduling](COMPONENTS/SCHEDULING.md). |
| Shared HTTP/file dispatch | `Proxy.run_scheduler`; [scheduler.py](../file_proxy/scheduler.py): `HttpQueue.enqueue/peek/pop/_candidate/cancel/finish/close`, `HttpJob` | HTTP handler/lifecycle tasks and async runtime; impacts non-preemption, ordering, continuation grace, shutdown and admission release. [Shared scheduler scenario](../tests/test_e2e.py): held file work, queued HTTP work, streaming and bypass. |
| HTTP routing, limits, streaming | [http_proxy.py](../file_proxy/http_proxy.py): profiles/route constants, `HttpProxyConfig.validate`, `AdmissionController`, `HttpProxyService.handle/_read_body/_relay/_route_is_queued`, `run_http_proxies` | CLI configs -> aiohttp listeners -> queue/upstreams; impacts clients, headers, body accounting, overload and diagnostics. [Shared scheduler scenario](../tests/test_e2e.py): streamed bytes, request IDs and route bypass. [HTTP transport](COMPONENTS/HTTP_TRANSPORT.md). |
| ComfyUI identity, settlement, cancellation | `_prepare_prompt_id`, `_workflow_signature`, `comfyui_resource_key`; `HttpProxyService._run_prompt_lifecycle/_wait_for_prompt_completion/_cancel_and_reconcile/reconcile_records` | Prompt HTTP routes; impacts unique IDs, client disconnect, scheduler slot duration, managed/external ownership and restart ambiguity. ComfyUI lifecycle scenario checks held slot, keyed history and durable cached outcome. |
| Durable HTTP outcomes, history, artifacts | [store.py](../file_proxy/store.py): `ExecutionStore`; `HttpProxyService._archive_completion/_archive_artifact/_remove_managed_output_duplicates/_special_route`; `_managed_output_path` | HTTP lifecycle, cached history/view/job/status routes, archive/shutdown eviction; impacts retention, prompt uniqueness and managed output deletion. ComfyUI lifecycle scenario verifies cache retrieval after restart; eviction is not a separate scenario. [DATA_MODEL.md](DATA_MODEL.md). |
| GPU process ownership and health | [backend.py](../file_proxy/backend.py): `BackendManager.prepare_for/ensure_comfyui/stop_comfyui/released/health_status`, `_unload_ollama`, Windows containment helpers; `Proxy._prepare_resource` for Forge | Async scheduler/runtime -> external services and owned subprocess; impacts startup, idle residency, cooldown, handoff and logs. Controlled lifecycle and opt-in live scenarios verify managed handoff and backend residency. [Managed runtime](COMPONENTS/MANAGED_RUNTIME.md). |
| Alias effort normalization | [reasoning_policy.py](../file_proxy/reasoning_policy.py): `ReasoningPolicyStore.refresh/normalize/_validate/status` | HTTP queued Ollama route and status; runtime config selects file. Affects one request field, header/body length, hot reload and model alias compatibility. [Policy-through-HTTP scenario](../tests/test_e2e.py): mapped effort, unchanged task content, reload and last-known-good status. [Reasoning policy](COMPONENTS/REASONING_POLICY.md). |
| HTTP diagnostics | [diagnostics.py](../file_proxy/diagnostics.py): `RequestDiagnosticLog.write/_cleanup`; `StreamDiagnostics`, `_write_diagnostic`, `_relay` in HTTP module | Tracked Ollama inference dispatch/terminal events; impacts body-free diagnostics and retention. [Shared scheduler scenario](../tests/test_e2e.py) verifies request IDs and streaming; diagnostic rotation/expiry is not a separate scenario. |
| Dashboard and restart lifecycle | [supervisor.py](../file_proxy/supervisor.py): `RuntimeSupervisor.start/stop/restart/snapshot`, `RuntimeSnapshot`; [tui.py](../file_proxy/tui.py): `ProxyDashboard`, `_render_snapshot`, lifecycle actions | CLI `tui`, Textual timers/key actions; impacts listener rebinding and safe worker shutdown. Dashboard scenario verifies recovery, stop waiting for a subprocess and restart; rendering and shortcut layout are not separate scenarios. |

## Outside this repository

There is no built-in publisher, harvester, task prompt generator, image workflow
builder, or subscriber database. The [README integration guide](../README.md)
contains examples; [zet.json](../registries/zet.json) registers
`AI_Manager.ollama_proxy_worker` and `AI_Manager.local_image_proxy_worker` in a
separate Zet checkout.
Inspect the relevant subscriber repository before planning changes to its task
behavior. Do not assume README example code is imported here.

## Native AI boundary (2026-10-16)
- `ai_worker.py` owns generic Codex/Ollama/ComfyUI invocation and result manifests.
- `Proxy.cloud_dispatch` owns trusted cloud admission; local scheduler and resource
  affinity exclude Codex. Runtime configuration owns cloud capacity and Codex names.
- `model_catalog.py` owns shared capability metadata, with HTTP metadata access in
  `_special_route` and file refresh requests hosted by the independent dispatcher.
- `test_cloud_jobs_run_alongside_local_work_and_recover_without_replay` is the
  approved sixth isolated scenario. See [native providers](COMPONENTS/AI_PROVIDERS.md).
