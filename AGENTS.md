# Agent operating instructions

AI_Proxy is a Python transport and execution scheduler for application-owned
workers, queued Ollama HTTP requests, and optional ComfyUI HTTP requests. It
serializes expensive work on a designated worker machine; subscribers own task
prompts, payload schemas, business state, retries, and result harvesting.

Start at [Docs/INDEX.md](Docs/INDEX.md). Read the relevant documents before
significant changes, then inspect their authoritative source and tests. The
existing `Docs/` capitalization is intentional; do not create a second `docs/`
tree. [README.md](README.md) remains the subscriber integration guide.

## Working agreements

- Explain/review/diagnose/plan requests are read-only unless edits are requested.
- Build/change/fix requests authorize scoped local edits and non-destructive
  validation. Ask before destructive actions, external writes, new production
  dependencies, or material scope expansion.
- Preserve user changes; exclude unrelated cleanup. Use one agent unless the
  user explicitly requests delegation. Search before reading broadly.
- Follow existing Python patterns: dataclasses for contracts, pathlib paths,
  argparse CLI, aiohttp/asyncio for HTTP, pytest with `asyncio.run` for scenarios.
  Prefer existing dependencies and tools; no configured lint/type-check suite.
- Resolve material ambiguity from code/tests first; otherwise ask one concise
  question. Never silently treat an assumption as implemented behavior.
- Keep a small suite of representative end-to-end tests for current behavior.
  Do not add tests or expand existing scenarios into new test cases, branches,
  parameter matrices, compatibility coverage, or coverage targets without Joe's
  explicit approval. This also applies to tests Codex proposes after a defect.
  Ask before authoring the test; continue implementation and existing validation
  without it when feasible. Updating helpers, assertions, and synchronization
  inside an approved scenario is allowed when its intent stays the same. Joe's
  approved baseline is five isolated end-to-end scenarios and one explicitly
  gated live backend lifecycle scenario. No unit tests are authorized beyond it.

## Commands

Use the project virtual environment, not system Python. From the repository root:

```powershell
.\.venv\Scripts\python.exe -m file_proxy.cli --help
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/test_e2e.py::test_file_jobs_wait_for_complete_inputs_and_publish_terminal_results -q
.\.venv\Scripts\python.exe -m pytest tests/test_e2e.py::test_comfyui_settlement_handoff_and_cached_result_survive_restart -q
git diff --check
```

Setup and operational commands, command verification status, and the observed
baseline test failure are in [Docs/DEVELOPMENT.md](Docs/DEVELOPMENT.md). Do not run
`once`, `run`, or `tui` against a real queue just to validate a code change.
The live backend scenario is opt-in; see [DEVELOPMENT.md](Docs/DEVELOPMENT.md).

## Mandatory boundaries

Preserve [Docs/INVARIANTS.md](Docs/INVARIANTS.md): immutable published envelopes,
safe relative paths and inventories, trusted local command registration with
`shell=False`, single-attempt execution, non-preemptive shared scheduling,
backend ownership, and cache-before-history publication. The singleton lock and
execution metadata are local runtime state, not distributed synchronization.
Operate one assigned proxy per queue; do not infer distributed safety from a
local lock. Do not add proxy-wide system prompts or subscriber-specific imports.
Reasoning-policy rewriting has a narrow documented scope; do not generalize it
to task prompt rewriting.

## Planning and documentation maintenance

Before significant implementation: read this file, consult the index, retrieve
only relevant architecture/component/data/invariant docs, inspect code and tests,
and identify affected docs. Cross-component features, complex fixes, and refactors
require a written plan before code changes. Use [plans/README.md](plans/README.md)
and break every plan into Luna-sized work packets; trivial edits need no plan.

During implementation: synchronize the active plan, record discovered constraints
and dependencies, investigate code/doc contradictions, and record deviations.
Keep unrelated docs untouched; do not invent historical rationale.

After implementation: update affected architecture, interfaces, data, contracts,
or workflows; update the code map when responsibilities/entry points change; add
an ADR for a significant durable design choice. Run the smallest relevant existing
checks, inspect the final diff, and validate documentation links. Record observed
results, complete/archive the plan, and report changed files, failures, unverified
claims, and remaining issues. Documentation maintenance is part of completion;
incidental implementation details do not warrant new documentation.
