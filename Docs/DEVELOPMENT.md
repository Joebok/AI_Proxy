# Development and verification

## Prerequisites and setup

[pyproject.toml](../pyproject.toml) declares Python >=3.10, setuptools build,
aiohttp and Textual runtime dependencies, and pytest in `[dev]`. Use a local
`.venv` for installation and execution. Observed environment: Windows/PowerShell,
Python 3.14.6. The code uses `datetime.timezone.utc` for compatibility with the
declared Python 3.10 minimum. Python 3.10 runtime validation remains unavailable
on this host.

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
the current environment. Before the test-suite replacement, the legacy suite
passed 69 tests and skipped one Windows symlink test. Its individual checks were
initially represented by five end-to-end scenarios in the 2026-10-11 baseline.
The maintained suite now has six isolated scenarios; the sixth was separately
approved and added on 2026-10-16:

| Command | Observed status |
| --- | --- |
| `.\.venv\Scripts\python.exe --version` | Passed; Python 3.14.6. |
| `.\.venv\Scripts\python.exe -m file_proxy.cli --help` | Passed; parser exposes `run`, `tui`, `once`, `status`, `validate-subscriber`. Does not load registries or touch a queue. |
| `.\.venv\Scripts\python.exe -m pytest -q` | Historical 2026-10-11 baseline: 5 isolated end-to-end scenarios; the live backend scenario was deselected. Current count and results are recorded in later dated entries below. |

The replacement policy scenario advances and verifies the policy file timestamp
before checking reload. It does not change application reload logic. The earlier
intermittent reload assertion did not reproduce in the pre-replacement baseline.
Select an isolated scenario by name as needed. The opt-in live check loads and
unloads an installed Ollama model and starts managed ComfyUI. Stop the normal
proxy first and choose a model that fits available GPU memory:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_e2e.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_e2e.py -k dashboard -q
.\.venv\Scripts\python.exe -m pytest tests/test_live_backends.py -q --live-backends --live-runtime-config .\runtime-config.local.json --ollama-test-model qwen3:4b-q4_K_M
git diff --check
```

The default suite uses temporary envelopes/runtime directories, loopback fake
services, and disposable worker/backend processes. The live check refuses to
run if Ollama already has a resident model, if the normal proxy owns its local
runtime lock, if the installed model/configuration is missing, or if ComfyUI
cannot be safely started. It never runs a workflow or text generation. It may
use GPU memory while active. Live validation was not run during this replacement
because the normal proxy currently owns the lock.

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
The included registry requires `ZET_PROJECT_ROOT`;
the standard launchers supply a default Zet root. For an isolated test
deployment, select a registry directory containing only the intended registrations.

[run_file_proxy.ps1](../run_file_proxy.ps1) and [run_file_proxy.bat](../run_file_proxy.bat)
launch `tui` using venv Python. Both default the Ollama listener to `11433`, upstream
`11434`, queue to `E:\Zet\AI_Queue`, and Zet checkout to a sibling.
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
| Native ComfyUI jobs held across restart | Inspect local `unsettled_comfyui.json`. Managed scheduler now stops/verifies its owned backend, archives the guard and resumes automatically; the failed/interrupted attempt stays terminal. External ownership or stop/archive failures retain the hold. See [recovery decision](DECISIONS/0005-managed-native-comfyui-recovery.md). |
| Native ComfyUI guard sharing violation | Worker JSON replacement/settled guard deletion and managed guard archival retry `PermissionError` for up to 15 seconds. Persistent errors fail closed: keep durable ambiguity evidence and do not replay the prompt. |
| Dashboard error/restart | Supervisor logs/listener bind state; `S` stops, `R` restarts, `End` follows logs, `Q`/Ctrl+C exits. Active file workers finish before safe stop. |
| Policy does not reload | Optional path, JSON validation/status error, last-known-good state, timestamp change detection; inspect observed failing test. |

Headless logs use `[file-proxy]` / `[http-proxy]`; dashboard logs are bounded in
memory. Local `request-diagnostics` records track Ollama inference streams.
`comfyui.log` captures backend output; worker stdout/stderr are captured in full
in result JSON, so bounded/no-secret worker logging is a responsibility, not an
enforced truncation guarantee.

## Coverage gaps and further investigation

The small functional suite demonstrates representative current flows; it is not
a branch-by-branch contract inventory. It does not directly verify every
priority/barrier combination, journal crash window, cache eviction, external
unresolved cancellation/restart, real inference/rendering, Dropbox sync, Linux
process-tree cleanup, or subscriber harvest behavior. Follow the approval rule
in [AGENTS.md](../AGENTS.md) before adding a test to address such a gap; tests
are not the default response to every code change.

## Documentation validation workflow

Check relative Markdown targets resolve from each document, source modules and
named symbols exist, and new documents are reachable through the index. Inspect
code for assertions that sound stronger than its checks. Run `git diff --check`
and inspect the scoped documentation diff; remember untracked documents are not
included in ordinary `git diff`. Compare final source/test/config state with the
starting checkout so pre-existing work is preserved. Record failures and commands
not exercised instead of marking all instructions verified.


## Unified AI jobs validation (2026-10-16)

`.\.venv\Scripts\python.exe -m pytest -q`: **6 passed, 1 live scenario deselected (9.45s)**. The explicitly approved new isolated scenario
is included. Python AST and changed browser-script syntax checks, introduced Markdown
links, and `git diff --check` passed. Disposable prepared-response checks covered
restart continuation, duplicate application, incomplete transfer, retained harvest
failure/retry, ownership fencing, offline/malformed model catalogs and recoverable
history. A transient Windows state-read sharing violation was found and repaired
before the final Zet baseline passed. No dependencies or live backend calls were
added. Upgrade AI_Proxy registrations before migrated Zet; no production rollout
was performed. See the [completed plan](../plans/completed/2026-10-16-unified-ai-jobs.md).


## Native worker interpreter correction (2026-10-16)

Recent creative Codex/Ollama attempts failed at worker launch. Reproducing the
shipped `python3 -m file_proxy.ai_worker --help` command showed system Python lacked
`aiohttp`, while the proxy virtual environment succeeded. Native registrations now
resolve the running interpreter and package directory through reserved registry
placeholders. Legacy registrations are unchanged. Nonzero worker exits retain a
bounded stderr tail in their transport error for future durable receipts.

Disposable smoke checks loaded the shipped registry and invoked all three native
commands with `--help`: passed without provider calls. The existing proxy suite
passed: 6 passed, 1 live scenario deselected (9.10s). Python AST, registration JSON
and `git diff --check` passed. No runtime restart, failed-job replay or production
backend call was performed. Restart the proxy to load registrations and explicitly
retry affected workflow jobs; their original failed receipts remain history.


## Native ComfyUI guard recovery (2026-10-16)

A stale local submission guard blocked E:\Zet\AI_Queue after a drive outage.
Read-only live status confirmed no running file worker, no managed process and
an unreachable ComfyUI endpoint. The guard was renamed locally to
`unsettled_comfyui.recovered-20261016.json`; the existing proxy resumed, started
ComfyUI and processed a render whose keyed history reported completed success.
The next image job started. No failed attempt was replayed or envelope edited.

The managed scheduler now automatically stops/verifies its owned backend before
archiving a native guard and resuming admission. Existing isolated checks passed:
`.\.venv\Scripts\python.exe -m pytest -q`: **6 passed, 1 live scenario deselected
(8.46s)**. The new native guard branch is not directly covered by those scenarios;
no test scenario was added or expanded. The active process was left running;
code recovery takes effect on the next full proxy application launch.

## Storage recovery verification (2026-10-17)
The six approved isolated scenarios passed with `.venv/Scripts/python.exe -m pytest -q`
(one live scenario deselected). Python compilation and `git diff --check` passed.
No maintained scenario was added or expanded. Controlled private-worker checks
also verified parent-proxy death, surviving local slot ownership with independent
cloud execution, queue disappearance/reconnect, and publication of successful
orphan execution without replay. Windows process handles retained by a launcher
no longer make an exited proxy appear alive.

Native submission identity/phase and outcomes are local before shared writes;
metadata excludes the workflow graph. One background probe worker per monitor
bounds status wait time and leaves unrelated verified resources usable during a
stalled device call. Physical drive disconnects, live providers and production
restart/deployment were not exercised. See [ADR 0006](DECISIONS/0006-storage-outage-recovery.md).


## Independent code review remediation (2026-10-22)

The six existing isolated scenarios passed (`6 passed, 1 deselected`). Python
compilation and `git diff --check` passed. Disposable checks verified Windows
junction rejection and safe INVALID publication, ambiguous/PREPARED/corrupt
guard behavior, corrupt storage journal blocking, and Answer collision
preservation. Relative Markdown links in the changed docs resolved. No tests
were added or expanded, no real queue or live backend was used, and Python 3.10
runtime validation remains unavailable on this host. The opt-in live scenario
was deselected.
