# Data, configuration, and ownership

Definitions in [models.py](../file_proxy/models.py), [runtime.py](../file_proxy/runtime.py),
[http_proxy.py](../file_proxy/http_proxy.py), and [store.py](../file_proxy/store.py)
are authoritative. This document explains contracts and lifecycle without copying
complete schemas. There is no subscriber business database in this project.

## Filesystem protocol

| Entity | Definition / ownership / lifecycle |
| --- | --- |
| `job.json` | `JobManifest`: version 1, subscriber/job/worker identity, UTC `created_at`, declared `files`, optional `resource_key`, optional `priority` (default 0). Subscriber writes last inside staging; immutable once published. |
| File inventory record | `validation.validate_file_inventory`: relative `path`, nonnegative integer `size` (not bool), 64-character `sha256`; actual digest compared case-insensitively. No separate hex-format parser. Missing/locked/mismatched files defer execution rather than immediately invalidate. |
| Subscriber payload/output | Application-defined files; proxy recursively inspects JSON path references but does not impose task schemas. Workers may add outputs but must preserve inventoried inputs and manifest identity. |
| `proxy_result.json` | `ProxyResult`: identity, worker, terminal status, timestamps/duration, exit code, error type/message, stdout/stderr, output inventory. Proxy-owned; subscriber verifies/copies results before retention. |
| Queue roots | `Proxy.__init__` normalizes a parent root or an existing case-insensitive `File_Proxy` directory. States are `Ask/<subscriber>/<job>`, `Running/...`, `Answer/...`; `Control/` is still created but is not the current lock location. |

Identifiers match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. Directory identities must
match the manifest. `parse_utc` accepts only timestamps with an explicit **zero**
UTC offset (`Z`/`+00:00`); other offsets and naive times are rejected.

`priority` is an integer from 0 to 100 (bool rejected). `resource_key` may be null
or a nonempty, trimmed string of at most 512 characters without characters below
ASCII 32. Legacy `image:comfyui` / `image:comfyui:*` keys normalize to `comfyui`
/ `comfyui:*`; other keys are unchanged. Trusted registry `backend` takes
precedence over a recognized resource prefix. Ordering is owned by
[SCHEDULING.md](COMPONENTS/SCHEDULING.md).

Path handling is defined by `validate_relative_reference` and `_path_values`:
reject blank/absolute/traversal/escaping references under both Windows and POSIX
rules. JSON keys `path`, `_path`, `_file`, `_files`, `_dir` are recognized
recursively, except `api_path`, `endpoint_path`, `url_path`. Use POSIX-style
relative values for portability. Not every arbitrary string is validated as a
path; this is a scoped reference convention, not a full filesystem sandbox.

`build_file_inventory` skips files whose basename starts with `.` and explicit
exclusions. Result inventory excludes `proxy_result.json`, includes `job.json`,
original inputs, and non-hidden output files. It is not just an output artifact
list. Request inventory need not list itself and can be empty, so publishers
must inventory material inputs themselves. See [README](../README.md) examples.

File terminal statuses are uppercase `SUCCEEDED`, `FAILED`, `INVALID`; HTTP
states below are a separate contract. Preserve unknown future error types.
Current engine error types include `INVALID_JOB`, `WORKER_EXIT`, `WORKER_TIMEOUT`,
`WORKER_LAUNCH`, `INVALID_OUTPUT_STATE`, `PROXY_INTERRUPTED`, `RESOURCE_TRANSITION`.

## Trusted worker registry

[Registry.load](../file_proxy/registry.py) reads all top-level `*.json` in the
registry directory. Each file has a subscriber ID (defaults to filename stem)
and nonempty `workers` mapping. Duplicate subscribers and invalid worker names
fail startup. [WorkerRegistration](../file_proxy/models.py) defines command
argument array, positive timeout (`timeout_seconds`, with `timeout` fallback),
optional working directory, and optional `backend` (`ollama`/`comfyui`).

`${NAME}` substitution uses the proxy process environment in string/list values;
missing variables fail loading. Configured cwd must already exist. Relative cwd
is interpreted by the host process, not relative to the registry file. Registry
validation lists worker names; it does not execute commands or prove dependencies
are installed. Registry is loaded once; restart after changes.

Included registrations: [Zet](../registries/zet.json) declares trusted backend
families; [Checkpoint Lab](../registries/checkpoint_lab.json) has no backend
declarations. Managed-mode jobs must resolve a backend via registration or a
recognized resource key; otherwise they are held.

## Runtime and listener configuration

`RuntimeConfig` and nested `ManagedComfyUIConfig` are frozen dataclasses. JSON
loading rejects unknown keys, converts managed arguments to a tuple, and validates
enabled managed executable/cwd/optional files and declared input/output directories.
Do not assume exhaustive type/range validation for every field; follow the code.

Relative runtime, policy, executable, script, cwd, and model-config paths resolve
against the config file's directory, or current working directory without a
config. Managed `arguments` strings are not expanded/resolved by `resolved`;
prefer absolute input/output paths. No `${NAME}` expansion for runtime JSON.

CLI explicit limits override config for streak, settlement timeout, admitted
request count, buffered body bytes, and queue wait. `--comfyui-upstream` overrides
the nested upstream; `--external-comfyui` disables managed ownership **after**
`RuntimeConfig.load` validation, so an invalid enabled managed config still fails
before that override. `once` does not enable backend management.

| Setting family | Authoritative default / meaning |
| --- | --- |
| Runtime storage | `.ai-proxy-runtime` relative to config base/cwd; example config uses a machine-specific absolute path. |
| Admission | 32 queued/active admitted requests, 256 MiB aggregate buffered bodies, 1,800 s queue wait. Shared across listeners in `run_http_proxies`. |
| Listener | `HttpProxyConfig`: 100 MiB per request; CLI listener disabled unless a port is passed; host defaults loopback. Upstream defaults Ollama 11434, ComfyUI 8188. |
| Settlement | 7,500 s generation/upstream timeout, 30 s cancellation grace; listener history polls default 1 s. |
| Scheduling | Streak 5, HTTP oldest override 60 s; file polling 1 s and sync grace 300 s from CLI. Listener continuation grace 2 s. |
| Retention | Lifecycle/cache terminal records 7 days, cache 10 GiB; HTTP diagnostics 3 UTC days. Cache eviction runs during archive and shutdown, not a permanent background sweep. Diagnostics cleanup runs on logger construction and day changes. |
| Managed process | Disabled by default; startup deadline 180 s, cooldown 30 s, idle stop 60 s, shutdown deadline 15 s; logs rotate at 4 MiB with 3 backups. |
| Reasoning policy | Optional `ollama_reasoning_policy_path`; absent means no alias normalization. Format and reload contract: [REASONING_POLICY.md](COMPONENTS/REASONING_POLICY.md). |

[runtime-config.example.json](../runtime-config.example.json) is a local deployment
example, not a portable ready-to-run config. Launchers map environment variables
to CLI options; see [DEVELOPMENT.md](DEVELOPMENT.md).

## Local persistence and lifecycle

| Runtime path | Definition / owner |
| --- | --- |
| `proxy.lock` | `RuntimeLock`: exclusive-create JSON with PID, hostname, start time. Released on normal exit. Foreign-host owners are not reclaimed. This is local process coordination, not a queue-wide distributed lock. |
| `worker-outcomes/<subscriber>/<job>.json` | Engine journals normal worker outcomes before writing the transport result; removed after successful Answer move. Recovery can republish without rerunning a completed worker. Invalid/interrupted outcomes do not use this normal-worker journal path. |
| `executions.sqlite3` | `ExecutionStore.__init__` creates `executions` and `artifacts`; no versioned schema migrations are implemented. Request bodies are not stored. |
| `cache/<prompt_id>/<hash>-<filename>` | Archived standard ComfyUI artifacts fetched via `/view`; `.partial` write becomes final by replace. Mapping is stored in artifacts table. |
| `comfyui.log` / backups | Backend process stdout/stderr, size-bounded rotation; not the same privacy contract as HTTP request diagnostics. |
| `request-diagnostics/requests-YYYY-MM-DD.jsonl` | `RequestDiagnosticLog`: UTC rolling dispatch/terminal diagnostics; callers record identity, route, transport reason, elapsed time, byte/chunk counts. No request/response bodies or headers at current call sites. |

`executions` identifies HTTP work by UUID `request_id`, optional globally unique
`prompt_id`, backend, free-form state/outcome, error, timestamps, and optional
history JSON BLOB. `artifacts` is keyed by `(prompt_id, filename, subfolder, type)`
with cache path and size. There is no SQL foreign key; code maintains deletion
relationships. Consult the SQL directly before a schema change and plan existing
runtime database compatibility.

States written include `accepted`, `dispatched`, `cancelling`, `blocked`,
`completed`, `failed`, `cancelled`. Only the last three are terminal for `pending`
and eviction. These are strings, not an enforced SQL state machine. Ollama
inference records begin at dispatch; ComfyUI prompt records begin before upstream
dispatch. Not every admitted route has a durable record.

Successful/failed archived keyed history can be served locally; aggregate managed
history includes only completed records. Default clear-history deletion selects
completed records; explicit ID deletion uses the requested IDs. Prompt uniqueness
is scoped to retained records: deletion/eviction removes the uniqueness record.
Artifact lookup without prompt ID chooses the newest matching metadata row.

Retention deletes terminal records and associated files oldest-first for age or
capacity; pending records remain. Cache byte accounting uses metadata sizes, not
a filesystem scan. Full transactional filesystem/database recovery and orphan
cleanup are not established; do not promise them.
