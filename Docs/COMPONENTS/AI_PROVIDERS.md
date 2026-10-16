# Native AI providers and independent cloud execution

Introduced 2026-10-16 for the approved Zet unification plan. Subscriber task prompts,
reference preparation, graph compilation, result application and retries remain
subscriber-owned. This repository owns reusable provider execution and scheduling.

## Worker/request interface

[ai_worker.py](../../file_proxy/ai_worker.py) is a one-job CLI:
`python3 -m file_proxy.ai_worker --provider codex|ollama|comfyui --job-dir <folder>`.
Registrations are trusted local argv, with `shell=False`; jobs cannot choose commands
or a scheduling lane. `ai_request.json` version 1 is an inventoried payload inside
the unchanged File Proxy protocol-v1 envelope. It contains provider/model, frozen
prompt/system or graph/bindings, ordered images, output/schema, execution options
and opaque owner metadata. No subscriber modules are imported by native providers.

Codex uses the installed authenticated CLI with read-only sandbox, no approvals,
ephemeral execution, explicit model, images and optional output schema. Its bounded
noninteractive interface follows the [official documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
Ollama supports chat/generate with schema and ordered image bytes, preserving the
request's system/task wording. It does not rewrite prompts or select another model.
ComfyUI validates required graph nodes after backend preparation, materializes input
bindings through upload responses, journals submission identity, waits for keyed
terminal history and copies the output into the envelope before terminal publication.

Workers write `answer_manifest.json` and `ai_result.json` with success/failure,
runtime/model evidence, timestamps and bounded error text. Proxy transport results
and inventories remain engine-owned. Subscriber-side application must check both
the transport result and application result.

## Scheduling and failure ownership

Trusted registry `backend=codex` selects the independent cloud dispatcher.
`RuntimeConfig.cloud_concurrency` defaults to two positive integer slots. It keeps
scanning while a local file worker or HTTP generation occupies the local slot.
Cloud execution never calls BackendManager or updates local resource affinity.
Filesystem-only, HTTP/managed and TUI runtimes host the same cloud dispatcher;
`once` still executes just one selected job. Shutdown waits for admitted workers.
Restart publishes interrupted attempts as failures without replay.

Local Ollama/ComfyUI ordering, affinity, ownership and non-preemption remain intact.
An ambiguous native ComfyUI submission saves `unsettled_comfyui.json` in local runtime
state, blocking subsequent local admission across restart while cloud work can
continue. Reconcile the saved prompt/stop its backend before explicitly clearing
that guard; restarting alone must not imply the GPU is free.

## Capability metadata

[model_catalog.py](../../file_proxy/model_catalog.py) publishes
`Control/model_catalog.json` atomically. Native runtimes refresh on startup and
every five minutes, independently of GPU admission. `Control/refresh-models.json`
requests an earlier refresh; configured HTTP listeners expose GET/POST
`/_proxy/models`. Metadata-only requests are not inference jobs.

Codex model names have one configured owner: `RuntimeConfig.codex_models` (bare
names in runtime JSON). Ollama capabilities come from tags/show metadata, never
model-name guessing. ComfyUI node/model options come from object-info metadata.
Provider discovery errors retain that provider's last-known-good entries and are
reported to subscribers. ComfyUI can remain stopped until admission: registered
graphs may compile offline, but execution validates actual nodes after startup.

## Compatibility and validation

[registries/zet.json](../../registries/zet.json) adds `ai_codex`, `ai_ollama` and
`ai_comfyui` for new envelopes. Existing `ollama`/`local_image` registrations remain
only to settle immutable legacy asks that still require their old app-owned adapter.
Upgrade the proxy before submitting native envelopes; do not rewrite live asks.

The approved sixth isolated E2E scenario uses a controlled Codex executable and
workers to verify the frozen text/image/schema interface, cloud/local overlap,
two-slot admission, third-job deferral, terminal publication and interrupted recovery.
Existing ComfyUI HTTP settlement/cache and non-preemption scenarios remain intact.
No live provider access or production rollout is established by these scenarios.
