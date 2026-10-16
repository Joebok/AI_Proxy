# Unified AI requests, execution, harvesting, and model selection
Status: completed (local implementation; production rollout excluded)
Related specification: Joe's approved implementation plan in this chat, 2026-10-16.

## Goal and constraints
All active creative AI calls use AI_Proxy execution and scheduling, common Zet
submission/harvesting, durable history, and shared capability-aware model controls.
All universes are shown by default; cloud concurrency defaults to two; background
collection runs independently of page visits. No production deployment, live AI
calls, new production dependencies, retired workflows, or delegated implementation.
Preserve AGENTS.md user changes, universe isolation, immutable inputs/locks, ordered
references, attempt fences and non-preemptive local scheduling.

## Implementation packets
Each packet is single-agent and Luna-sized. Dependencies follow the approved plan.
| Packet | Ownership / dependencies | Acceptance / validation | Status |
|---|---|---|---|
| WP1 contracts/inventory | Both plans; active callers and Docs | Every producer/collector/selector mapped | complete |
| WP2 dispatch | AI_Proxy engine/scheduler/runtime/registrations; WP1 | Independent two-slot cloud lane; existing proxy E2E | complete |
| WP3 Codex | Proxy provider worker; WP2 | Frozen text/vision/schema results; controlled CLI | complete |
| WP4 Ollama | Proxy provider worker; WP1/2 | Generic execution, no task prompt rewriting | complete |
| WP5 ComfyUI | Proxy provider worker; Zet render preparation; WP1/2 | Exact graph/reference/settlement/cache behavior | complete |
| WP6 catalog | Proxy metadata, Zet cache; WP3-5 | One capability catalog, last-good cache | complete |
| WP7 submission | FileProxyClient, lifecycle, job service; WP1 | Durable ownership before publication; recovery | complete |
| WP8 collection | Coordinator/handlers, CLI and lifespan; WP7 | Shared verification/locks/receipts, cross-universe background | complete |
| WP9 Narrative | Narrative services; WP3/7/8 | Shared submit/collect, S1/S3 | complete |
| WP10 images | Ad hoc/prompt generation/workspace; WP5/7/8 | Durable child jobs and background application | complete |
| WP11 analysis | Structured/reconstruction/true-up/wizards; WP6-8 | Shared jobs, representative prepared responses | complete |
| WP12 minors | Minor services/workflow; WP10/11 | No page-read dependency; preserve revisions/publication | complete |
| WP13 local pipelines | Body/Head/Assembly/Dressing/gates/ranking/improvement; WP7/8 | All calls/receipts shared, S2 | complete |
| WP14 selector | Shared popup and all LLM callers; WP6 | Exclusive Codex/Ollama, inheritance/vision checks | complete |
| WP15 Queue | Query service/routes/navigation/UI; WP7/8 | Immediate load/polling, all-universe history/filters | complete |
| WP16 completion | Compatibility/fixtures/tests/docs; all | No bypasses, isolated suites pass, final diff | complete |

## Contracts and migration inventory
Current requesters: NarrativeGenerationService; AIProxyService local render/gate
packaging; OllamaModelService structured calls; structured_ai_service Codex;
ImagePromptGenerationService; ImageReconstructionService/ImageTrueUpService;
CostumeWizardService; QuickCharacterWizardService/MinorCharacterWorkflow;
LocalBodyReferenceService, LocalHeadImageService, LocalCharacterAssetPipelineService,
LocalImageRankingService, LocalPromptImprovementService, GateTestRigService.
Current collectors: AIAnswerHarvester; NarrativeProxyService; AdHocImageGenerationService;
OllamaModelService blocking wait; local workflow runners; wizard session reads and threads.
Selectors: settings, Narrative, Minor Characters, reconstruction, gate test rig.
Zet owns compiled prompts/schema/reference bytes/render graph and domain application.
Proxy owns generic provider invocation and execution lanes. Published legacy jobs
must remain readable; no uncertain execution is replayed. Durable producer-local
request records plus receipts own activity history, never the derived library index.

## Documentation impact
Both repositories: architecture, code map, data model, invariants, development,
queue/scheduling components and ADR. Zet: Narrative/local character components and
Testing.md. AI_Proxy: README/transport/runtime docs. Only affected knowledge changes.

## Validation
Approved: existing Zet S1-S3 plus one new isolated unified queue/background/history/
selector scenario; existing five proxy scenarios plus one cloud/local concurrency
and restart scenario. Disposable migration/fault checks permitted. Commands:
`npm.cmd run test:e2e`, proxy `.venv/Scripts/python.exe -m pytest -q`, syntax checks,
and `git diff --check` in both repos. No live queue/model operations.

## Progress and decisions
- 2026-10-16: inspection confirmed missing AI Queue navigation loader, split
  collectors, direct Codex subprocesses, and completion deletion without receipts.
- Python 3.14 interpreter verified; AI_Proxy uses its own virtual environment.

## Final results
Local implementation completed with one agent and no new production dependencies.

- WP1–6: native versioned contracts, trusted cloud/local lanes, provider adapters,
  and proxy-owned capability catalog. Codex defaults to two concurrent executions.
  Zet never discovers selectable models directly; its compatibility listing API,
  popup and image controls read the proxy catalog/cache.
- WP7–8: durable request publication, common Inbox/receipts, cross-process locks,
  registered handlers, resumable continuations and cross-universe two-second host.
- WP9–13: active Narrative, image, analysis, wizard, Minor Character and local
  pipeline submissions/collection use the common transport and result services.
  Retired execution remains retired. Private Narrative Codex execution was removed.
- WP14–15: shared capability-aware popup and complete queue feed, navigation load,
  visible polling, retained rows on refresh failure and durable terminal history.
- WP16: bounded recoverable historical metadata adoption, compatibility bridges,
  approved isolated scenarios, affected architecture/data/invariant/component/code
  map/testing/development documents, and ownership ADRs.

### Observed validation

- Zet `npm.cmd run test:e2e`: 4 passed (34.2 seconds), including the approved new
  queue/background/history/selector scenario; private queues and prepared answers.
- Proxy `.\.venv\Scripts\python.exe -m pytest -q`: 6 passed, 1 opt-in live
  scenario deselected (9.45 seconds), including controlled two-slot cloud/local
  overlap, terminal publication, no replay and fake Codex text/vision/schema CLI.
- Disposable checks passed: continuation in a fresh service, duplicate application,
  incomplete answer deferral, retained/retried application failure, foreign universe
  rejection, cached catalog after malformed transfer, and attributed history import.
- Python AST, browser JavaScript syntax, introduced Markdown links and both
  `git diff --check` validations passed. Actual settings popup and Queue page were
  inspected with isolated Chromium screenshots. Windows concurrent state reads
  now retry brief sharing violations; a previously observed S2 read race was repaired.

### Compatibility and operating limits

Upgrade/restart AI_Proxy with its new registrations before running migrated Zet.
This checkout was not deployed and no live Codex/Ollama/ComfyUI check was run.
Legacy app-worker registrations deliberately remain only to settle already-published
immutable work; new jobs use native providers. Missing historical payloads and
ambiguous old direct execution are preserved for recovery rather than replayed or
invented. An unresolved ComfyUI submission blocks local admission durably until an
operator reconciles its prompt/backend; cloud execution continues. Old standalone
harvest intervals remain configured; the dashboard host uses two seconds.

The user’s pre-existing AGENTS.md edit was preserved. No commits, pushes or external
writes were performed.
