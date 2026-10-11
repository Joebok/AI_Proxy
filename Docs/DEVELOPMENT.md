# Development and verification

## Prerequisites and setup

[pyproject.toml](../pyproject.toml) declares Python >=3.10, setuptools build,
aiohttp and Textual runtime dependencies, and pytest in `[dev]`. Use a local
`.venv` for installation and execution. Observed environment: Windows/PowerShell,
Python 3.14.6. Python 3.10 compatibility is unverified: [reasoning_policy.py](../file_proxy/reasoning_policy.py)
imports `datetime.UTC`, which was introduced in Python 3.11. Do not claim the
declared minimum was tested or change dependencies/code as part of documentation.

Documented setup commands (not executed during the knowledge-base task):

```powershell
python3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --editable ".[dev]"
```

[setup_venv.ps1](../setup_venv.ps1) and [setup_venv.bat](../setup_venv.bat) create the
venv if absent, install editable development dependencies, and run tests. There
is no additional application build step, configured linter/type checker, or CI
workflow in this checkout. Reinstall after packaging/dependency changes. On POSIX,
the equivalent interpreter is `.venv/bin/python`; this platform was not exercised.

## Checks and observed results

Run from repository root. Only label commands verified when they succeeded in
the current environment. Baseline execution on 2026-10-11, before doc edits:

| Command | Observed status |
| --- | --- |
| `.\.venv\Scripts\python.exe --version` | Passed; Python 3.14.6. |
| `.\.venv\Scripts\python.exe -m file_proxy.cli --help` | Passed; parser exposes `run`, `tui`, `once`, `status`, `validate-subscriber`. Does not load registries or touch a queue. |
| `.\.venv\Scripts\python.exe -m pytest -q` | **Failed:** 68 passed, 1 failed, 1 skipped in 19.32 s. Failure: `test_policy_hot_reload_retains_last_known_good`, `KeyError: 'reasoning_effort'` at the first assertion after rapid policy rewrite. |
| `.\.venv\Scripts\python.exe -m pytest tests/test_reasoning_policy.py::test_policy_hot_reload_retains_last_known_good -q` | **Failed again:** same assertion, 0.37 s. Failure predates documentation edits. |

The skipped proxy test could not create a symlink in this Windows environment.
The reload implementation skips a file when `st_mtime_ns` equals the last loaded
value; timestamp-based change detection is a plausible cause of the failure,
not a confirmed diagnosis. No application or test changes were made to fix it.
See the [completed task record](../plans/completed/2026-10-11-agent-knowledge-base.md)
for final documentation validation results.

Select checks by affected capability using [CODE_MAP.md](CODE_MAP.md):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_proxy.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_http_scheduler.py tests/test_http_proxy.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_reliable_lifecycle.py tests/test_tui.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_reasoning_policy.py tests/test_diagnostics.py -q
git diff --check
```

These targeted group commands are suggested, not separately verified commands;
their tests were included in the full suite. Tests use temporary envelopes,
loopback fake servers, and disposable subprocesses rather than real GPU runtimes.
Review tests before running against an unusual environment.

## Operation and deployment

Operational examples below were inspected against CLI/launchers but **not run**:
they can consume real jobs, create queue paths, or contact backends. Use an
isolated queue/registry/runtime directory for acceptance checks. Replace paths
with local values; no production deployment is implied.

```powershell
.\.venv\Scripts\python.exe -m file_proxy.cli --root C:\path\to\AI_Queue --registry-dir .\registries run --http-listen-port 11433
.\.venv\Scripts\python.exe -m file_proxy.cli --root C:\path\to\AI_Queue --registry-dir .\registries tui
.\.venv\Scripts\python.exe -m file_proxy.cli --root C:\path\to\AI_Queue --registry-dir .\registries status --subscriber zet
.\.venv\Scripts\python.exe -m file_proxy.cli --root C:\path\to\AI_Queue --registry-dir .\registries validate-subscriber zet
```

Global `--root`, `--registry-dir`, `--runtime-config` precede the subcommand.
Listener/scheduler flags follow `run` or `tui`. `once` processes actual work and
does not construct managed backend lifecycle; use continuous managed mode for
managed file renders. Even `status` calls `ensure_layout` and may create queue
directories. `validate-subscriber` loads the entire registry before listing one
subscriber and does not test command launch.

Registry loading requires every referenced environment variable and cwd to exist.
Included registries require `ZET_PROJECT_ROOT` and `CHECKPOINT_LAB_PROJECT_ROOT`;
the standard launchers supply only a default Zet root. For an isolated test
deployment, select a registry directory containing only the intended registrations.

[run_file_proxy.ps1](../run_file_proxy.ps1) and [run_file_proxy.bat](../run_file_proxy.bat)
launch `tui` using venv Python. Both default the Ollama listener to `11433`, upstream
`11434`, queue to `%USERPROFILE%\Dropbox\AI_Queue`, and Zet checkout to a sibling.
They check Zet `config.toml` exists. They do not enable a ComfyUI HTTP listener.
PowerShell does not change cwd; BAT changes to repo root, so configless relative
runtime storage can differ when launched elsewhere.

Launcher environment options: `AI_QUEUE_ROOT`, `ZET_PROJECT_ROOT`,
`FILE_PROXY_SYNC_GRACE_SECONDS`, `AI_PROXY_RUNTIME_CONFIG`, `AI_PROXY_HTTP_PORT`,
`AI_PROXY_HTTP_HOST`, `AI_PROXY_OLLAMA_UPSTREAM`,
`AI_PROXY_HTTP_CONTINUATION_GRACE_SECONDS`, `AI_PROXY_HTTP_MAX_BODY_BYTES`,
`AI_PROXY_HTTP_UPSTREAM_TIMEOUT_SECONDS`. An unset/empty HTTP port is defaulted;
use CLI directly for filesystem-only operation. Other admission/queue limits are
runtime JSON or CLI options, not mapped by these launchers.

Managed mode requires reviewed machine-local configuration; the example ships
disabled and contains absolute paths. Real ComfyUI/Ollama, VRAM transitions,
model-path settings, Dropbox replication, and subscriber integrations were not
tested here. Keep code/model runtimes outside the synchronized queue and assign
one worker machine per queue. The lock is now local runtime state; it does not
coordinate different runtime directories or machines.

## Debugging and failure triage

| Observation | First inspection |
| --- | --- |
| All CLI commands fail loading registry | Missing `${NAME}` variables/cwd, duplicate subscribers, malformed JSON; inspect `Registry.load`. Loading is global. |
| Ask remains queued | Status diagnostics, inventory mismatch/sync grace, unknown worker, managed backend declaration, manual ComfyUI conflict or startup cooldown. Do not edit a published ask to repair it. |
| Running remains after worker finishes | Local worker outcome/result and Answer publication; rename retries handle sharing violations. Avoid launching duplicate work. |
| HTTP 413 / 503 / 504 | Per-body limit / shared admission or backend block / queue-wait or upstream deadline; inspect request ID/status and body-free diagnostics. |
| Backend shown unreachable | `/api/ps` and ComfyUI `/queue`; listener readiness does not prove backend readiness. `/system_stats` is not a render-success check. |
| Managed prompt unresolved | Durable lifecycle record, keyed history and cancellation branch; owned backend may be stopped, external ambiguous work retains slot. No replay. |
| Dashboard error/restart | Supervisor logs/listener bind state; `S` stops, `R` restarts, `End` follows logs, `Q`/Ctrl+C exits. Active file workers finish before safe stop. |
| Policy does not reload | Optional path, JSON validation/status error, last-known-good state, timestamp change detection; inspect observed failing test. |

Headless logs use `[file-proxy]` / `[http-proxy]`; dashboard logs are bounded in
memory. Local `request-diagnostics` records track Ollama inference streams.
`comfyui.log` captures backend output; worker stdout/stderr are captured in full
in result JSON, so bounded/no-secret worker logging is a responsibility, not an
enforced truncation guarantee.

## Coverage gaps and further investigation

Direct tests are absent or incomplete for manifest priority combinations, HTTP
barriers/oldest override, local-journal publication crash windows, full artifact
archive/eviction/output-deletion sequence, and external unresolved cancellation/
restart recovery. Existing fake-server/process tests do not establish real GPU,
Dropbox, Linux process-tree cleanup, or subscriber end-to-end correctness. Any
change in these areas should add targeted contract checks as part of its plan.

## Documentation validation workflow

Check relative Markdown targets resolve from each document, source modules and
named symbols exist, and new documents are reachable through the index. Inspect
code for assertions that sound stronger than its checks. Run `git diff --check`
and inspect the scoped documentation diff; remember untracked documents are not
included in ordinary `git diff`. Compare final source/test/config state with the
starting checkout so pre-existing work is preserved. Record failures and commands
not exercised instead of marking all instructions verified.
