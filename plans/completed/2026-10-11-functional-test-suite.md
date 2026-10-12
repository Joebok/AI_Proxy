# Replace the test suite with six functional scenarios

Status: complete

Owner: Codex

Started: 2026-10-11; updated: 2026-10-11

## Goal

Replace all seven current test modules (69 passing tests and one Windows-specific
skip in the current baseline) with five isolated end-to-end scenarios and one
opt-in live backend lifecycle scenario. Add a clear approval boundary against
unrequested tests. No application behavior, production dependencies, or
subscribers change.

## Current Behavior

Pytest's default discovery scans seven modules: file proxy, HTTP proxy and
scheduler, lifecycle, reasoning policy, diagnostics, and Textual dashboard.
Tests cover numerous individual branches and helper behavior. Existing backend
tests use loopback fakes and disposable processes. The project virtual
environment full-suite baseline passed 69 tests and skipped one symlink test;
the policy reload failure in earlier documentation did not reproduce.

## Desired Behavior

The default suite runs five end-to-end scenarios against temporary queues,
subprocess workers, real proxy services/supervisor/store, and controlled local
HTTP services; it cannot contact installed runtimes or modify machine-local
queue/runtime data. One explicitly enabled live scenario verifies Ollama
resident-model transitions and managed ComfyUI lifecycle/model discovery
without running a prompt or workflow. Agent instructions require Joe's explicit
approval before adding tests or new scenario scope beyond this baseline.

## Impact Analysis

| Files/modules | Interfaces/data/consumers affected | Tests and documentation |
| --- | --- | --- |
| Test package and pytest config | Pytest markers/options only; production interfaces unchanged | Replace all seven test modules with six scenarios and small shared harness. |
| Root agent instructions | Future test-authoring behavior | State scenario budget, explicit approval gate, and helper-maintenance allowance. |
| Development, code map, invariants, affected component docs | Test commands and actual evidence claims | Replace references to deleted tests; retain historical baseline records as historical. |

## Invariants and Constraints

Preserve published-envelope immutability, inventory/safe-path validation,
trusted registered commands, no worker replay, shared non-preemptive scheduling,
backend ownership, Ollama unload verification before ComfyUI startup, cached
artifacts before completed history, and policy rewrite scope. Use one agent.
Live checks must never stop an unowned backend or a production proxy. Normal
pytest must not open local runtime listeners or use machine-local runtime paths.

## Implementation Steps

1. **Packet 1: Replace the suite and build the isolated harness.** Own the test
   package and pytest configuration. Delete the seven inherited modules. Add
   shared temporary registry/envelope helpers, bounded polling, isolated fake
   services, and process cleanup. Preserve `asyncio.run` conventions and use
   existing dependencies. Checkpoint: pytest collects exactly six scenarios;
   harness endpoints/runtime paths are isolated and cleanup is reliable.
2. **Packet 2: Verify file-task lifecycle.** Own one end-to-end scenario using
   the CLI and registered subprocess worker. Publish an inventoried staged
   envelope, defer until an input arrives, publish a successful full inventory,
   then terminal worker-failure and unsafe-path outcomes. Depends on packet 1.
   Checkpoint: actual queue transitions and answer contents prove all outcomes.
3. **Packet 3: Verify dashboard lifecycle and recovery.** Own one scenario using
   Textual headless mode and real supervisor. Recover a seeded interrupted task
   without worker replay; stop while a worker is held, wait for completion,
   restart, process new work, then release lock. Depends on packet 1.
   Checkpoint: recovered answer, stopped/restarted runtime, and worker completion.
4. **Packet 4: Verify shared scheduling and streamed HTTP.** Own one scenario
   spanning held file work, queued/streamed HTTP work, and responsive discovery.
   Use synchronization events and queue snapshots rather than timing guesses.
   Assert bytes, IDs, order, and completion. Depends on packet 1. Checkpoint:
   observed wire behavior and non-preemption meet contracts.
5. **Packet 5: Verify reasoning-policy behavior through HTTP.** Own one scenario
   covering mapped input, unchanged task messages, file replacement/hot reload,
   malformed last-known-good recovery, status, and native route preservation.
   Advance/verify file mtime where needed; change no production logic. Depends on
   packet 1. Checkpoint: controlled upstream records expected payloads.
6. **Packet 6: Verify managed ComfyUI settlement and durable cache.** Own one
   scenario with a disposable managed process and controlled upstream lifecycle.
   Hold a prompt at acknowledged-but-unsettled keyed history while Ollama waits;
   publish success and a small artifact, verify cache publication before the
   Ollama handoff stops ComfyUI, then verify history/artifact after restart.
   Depends on packet 1. Checkpoint: settlement, handoff, and durable cache.
7. **Packet 7: Add gated live backend lifecycle.** Own a marked test plus three
   pytest options for opt-in, runtime config, and explicit installed Ollama
   model. Require local preconditions, explicitly reject a live production
   proxy lock, use temporary proxy state and outputs, and only clean up resources
   owned by this test. Verify model residency/load/unload, ComfyUI startup,
   ownership/model discovery, backend transition, and idle stop without a
   ComfyUI workflow. Depends on packet 1. Checkpoint: default collection
   deselects it; explicit validation reports clear precondition failures.
8. **Packet 8: Synchronize instructions and evidence.** Update AGENTS.md with
   the six-scenario approval baseline and the explicit approval rule; update
   development, code map, invariants, and relevant component docs. Remove links
   and commands that name deleted modules, preserving old plan records as
   historical. Depends on packets 2-7. Checkpoint: every current test reference
   resolves and documents distinguish tested behavior from coverage gaps.

## Validation

Run project virtual-environment pytest collection, each isolated scenario by
name, then the full default suite. Confirm normal collection excludes live
backends. Run the live scenario only if prerequisites are met; the current
runtime lock names PID 16004, so live validation is presently blocked on the
operator stopping the normal proxy. Run CLI help, Markdown relative-link
validation, and `git diff --check`; inspect tracked and untracked changes. Never
run real queue `once`, `run`, or `tui` as validation.

## Risks and Alternatives

Real GPU/model residency varies with driver and model installation. The live
test therefore makes assertions about reported residency and process lifecycle,
not generation correctness. Ollama model preload and immediate unload follow
the documented `/api/generate` empty-request and `keep_alive: 0` interface.
ComfyUI model discovery proves configured model paths are available without
loading a checkpoint. A required checkpoint-load workflow was explicitly
excluded. A running normal proxy must be stopped by the operator before live
validation; tests must refuse rather than stop it.

## Progress and Decisions

| Packet | Status | Discoveries, deviations, remaining work |
| --- | --- | --- |
| 1 | complete | Replaced all seven legacy modules with shared isolated fixtures and pytest configuration. Default collection contains exactly five scenarios and deselects the live scenario. |
| 2 | complete | File lifecycle scenario exercises staging, withheld input, registered subprocess success, failure, and invalid unsafe-path outcomes through the real CLI. |
| 3 | complete | Textual headless scenario verifies interrupted-task recovery without replay, stop waiting for active work, restart, listener rebinding, and lock release. |
| 4 | complete | Shared scheduler/HTTP scenario verifies non-preemption, streaming order and bytes, request IDs, and discovery responsiveness with controlled gates. |
| 5 | complete | HTTP policy scenario verifies effort mapping, unchanged messages, mtime-controlled reload, malformed last-known-good status, and native-route passthrough. |
| 6 | complete | Disposable managed ComfyUI scenario verifies unsettled history blocks Ollama work, cache/archive durability across restart, handoff, and post-cache cleanup. |
| 7 | complete | Added the opt-in live scenario and its pytest options. Live execution was not attempted because the normal proxy owns the local runtime lock (PID 16004); the test is explicitly gated and will not stop it. |
| 8 | complete | Updated AGENTS.md, development and architecture documentation, component references, decision record, and index. Relative Markdown links resolve. |

## Final Results

Changed paths: `AGENTS.md`; `pyproject.toml`; `tests/conftest.py`;
`tests/test_e2e.py`; `tests/test_live_backends.py`; removed the seven legacy
test modules; updated `Docs/DEVELOPMENT.md`, `Docs/CODE_MAP.md`,
`Docs/INVARIANTS.md`, `Docs/INDEX.md`, five component/decision documents, and
this plan record.

Validation observed: the project virtual-environment full default suite passed
with **5 passed, 1 deselected**; the dashboard scenario also passed separately
(1 passed, 4 deselected); pytest collection found five isolated scenarios and
deselected the marked live scenario by default; CLI help passed; relative-link
validation checked 22 Markdown files with zero missing links; `git diff
--check` passed. The previous seven-module baseline was 69 passed and 1 skipped.

The live test was not run: the local runtime lock belongs to the running normal
proxy (PID 16004). This is an intentional environmental blocker; the live test
must only be run after the operator stops that proxy. No installed backend,
real queue, or machine-local runtime state was used by the default suite. No
application behavior or dependencies changed.
