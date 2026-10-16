# Agent knowledge index

Read selectively: start with the row matching the task, follow its source links,
and inspect the corresponding tests. Code and tests define current behavior;
documentation records verified structure and explicitly labeled gaps.

## Document owners

| Document | Questions answered / when to read |
| --- | --- |
| [AGENTS.md](../AGENTS.md) | How must an agent work, plan, validate, and maintain knowledge? Read on entry. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | What runs, what owns state, and how do components communicate? Read for lifecycle or boundary changes. |
| [CODE_MAP.md](CODE_MAP.md) | Where is a behavior implemented, what calls it, and what tests/consumers are affected? Use before editing. |
| [DATA_MODEL.md](DATA_MODEL.md) | What contracts, identifiers, local persistence, and configuration exist? Read before changing formats or settings. |
| [INVARIANTS.md](INVARIANTS.md) | Which contracts must remain true, why, and how are they checked? Read relevant contracts before implementation. |
| [DEVELOPMENT.md](DEVELOPMENT.md) | How do I set up, run checks, debug failures, and distinguish verified commands from examples? Read for execution and validation. |
| [File transport](COMPONENTS/FILE_TRANSPORT.md) | How do asks become answers, what waits for sync, and how are interrupted jobs recovered? Read for envelope/worker changes. |
| [Scheduling](COMPONENTS/SCHEDULING.md) | Which job wins, what is affinity, and when is the execution slot released? Read for ordering/concurrency changes. |
| [HTTP transport](COMPONENTS/HTTP_TRANSPORT.md) | What routes queue, what gets rewritten, and how do streaming/prompt settlement/caching work? Read for API changes. |
| [Managed runtime](COMPONENTS/MANAGED_RUNTIME.md) | Who may start/stop backends, how is GPU residency coordinated, and how does the dashboard own runtime lifecycle? Read for backend/TUI changes. |
| [Reasoning policy](COMPONENTS/REASONING_POLICY.md) | What alias metadata rewrites `reasoning_effort`, and how does hot reload behave? Read for normalization changes. |
| [Transport boundary ADR](DECISIONS/0001-transport-and-local-state.md) | Why are worker code and application state outside the queue? Read for integration or persistence redesign. |
| [Execution ownership ADR](DECISIONS/0002-shared-execution-and-backend-ownership.md) | Why does a prompt hold the slot past its HTTP response, and who owns processes? Read for lifecycle redesign. |
| [Idle ComfyUI residency ADR](DECISIONS/0003-retain-idle-comfyui.md) | Why does managed ComfyUI stay loaded while idle, and which transitions still stop it? Read for residency policy changes. |
| [Planning convention](../plans/README.md) | When is a plan required, how large is a work packet, and how is it archived? Read before substantial work. |
| [Plan template](../plans/TEMPLATE.md) | What information makes a plan executable by another agent? Copy for substantial work. |
| [Knowledge-base completion record](../plans/completed/2026-10-11-agent-knowledge-base.md) | What was inspected/created and what was actually validated for this baseline? Read when auditing these docs. |
| [README.md](../README.md) | How does a subscriber publish, execute, and harvest jobs? Read for application integration; examples are not built-in subscriber implementations. |

## Task-oriented retrieval

| Task | Retrieve first |
| --- | --- |
| Understand startup or add a subsystem | Architecture -> code map -> relevant component -> invariants |
| Find a feature or debug a job | Code map -> file transport or HTTP transport -> development |
| Change manifest, result, database, or settings | Data model -> invariants -> related component/tests |
| Change ordering, priority, or affinity | Scheduling -> invariants -> shared scheduler end-to-end scenario |
| Change prompt generation | Subscriber worker owns it; read file transport, reasoning policy, and the transport ADR before choosing a boundary |
| Change managed ComfyUI or dashboard | Managed runtime -> HTTP transport for prompts/cache -> functional lifecycle scenarios |
| Plan a substantial refactor | Planning convention -> architecture -> impacted code-map rows and contracts |

## Evidence and retained notes

Baseline: inspected working tree on 2026-10-11, including existing uncommitted
source changes. Development records the test failure and untested deployment
claims. No historical design reasons are asserted beyond available evidence.

Existing user notes remain at [Docs-Joe/ComfyUI_Http_Proxy.md](../Docs-Joe/ComfyUI_Http_Proxy.md)
and [Docs-Joe/System_Prompt_Review.md](../Docs-Joe/System_Prompt_Review.md). They
provide integration intent, not primary current implementation documentation.
The ComfyUI note's strict HTTP priority and never-delete-original-output claims
conflict with current code; see scheduling and HTTP transport. The prompt note's
cross-repository claims were not verified in subscriber/ModelUpdater checkouts.

Each concept has one detail owner above. Add focused docs only for knowledge
that requires investigation or crosses component boundaries; add their retrieval
entry here. Keep code references relative and symbol-based, avoiding line numbers.

## Native AI integration (2026-10-16)
[Native AI providers](COMPONENTS/AI_PROVIDERS.md) owns the versioned request/result
payload, independent Codex lane, catalog/cache interface and legacy registration
policy. [ADR 0004](DECISIONS/0004-native-ai-and-cloud-lane.md) records the boundary.
