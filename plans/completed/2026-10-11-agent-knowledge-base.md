# Agent knowledge-base establishment

Status: completed

Owner: Codex

Started/updated: 2026-10-11

## Goal

Create accurate, selectively retrievable engineering documentation from the user's
attached requirements. Documentation only; preserve existing application changes,
deleted old docs and retained user notes. No dependency/build/test edits or external
writes. Entry: [AGENTS.md](../../AGENTS.md), [index](../../Docs/INDEX.md).

## Current Behavior

Repository had an extensive subscriber [README](../../README.md), source/tests,
deployment scripts/config examples and existing user notes in `Docs-Joe/`; no root
AGENTS, agent index or plan convention. Two tracked `Docs/` notes were already
deleted in the user's working tree. Source changes already modified ordering and
managed output cleanup. README/older notes did not consistently reflect them.

## Desired Behavior

An entering agent can find implementation, dependencies, contracts, relevant tests,
commands and maintenance rules without prior conversation. Claims trace to code;
uncertain history/deployment remain labeled. Local links exist; docs have distinct
owners. Plans use Luna-sized work packets.

## Impact Analysis

New root instructions, `Docs/` knowledge base and `plans/` convention/template.
Targeted README navigation/current-contract corrections. No runtime interface or
data changes. Existing source/test/script/config and `Docs-Joe/` state preserved.

## Invariants and Constraints

Documentation reflects the inspected working tree, not HEAD alone. Keep existing
`Docs/` capitalization and preserve deleted notes rather than recreate them. No
agent delegation. Do not promote intended integration rules to enforced guarantees.
Do not run real queue/GPU services for documentation validation.

## Implementation Steps

1. **Packet 1: Establish evidence.** Own read-only discovery of README, configs,
   entry points, modules, tests and launchers. Checkpoint: actual lifecycle/ordering/
   ownership understood; inspect relevant source and run existing suite/help.
2. **Packet 2: Create navigation and contracts.** Own AGENTS, index, architecture,
   code map, data model and invariants. Depends on packet 1. Checkpoint: each major
   behavior maps to source/tests, details have one owner; inspect traceability.
3. **Packet 3: Record subsystem and planning knowledge.** Own focused component
   docs, reconstructed ADRs, development guide, plan convention/template and README
   links/corrections. Depends on packets 1-2. Checkpoint: small-agent retrieval and
   future maintenance are executable; compare claims with inspected code.
4. **Packet 4: Validate and report.** Own documentation inspections only. Depends
   on packets 2-3. Check links/paths/symbols, Git whitespace/diff, new-agent navigation,
   and preservation hashes. Record baseline failure and unverified deployment.

## Validation

Observed baseline before edits: Python 3.14.6, CLI help passed; full pytest failed
with 68 passed, 1 failed, 1 skipped. Targeted failing reload test reproduced the
same KeyError. Source definitions/tests reviewed for queue transitions, backend
blocks, older-file arbitration, UUID settlement, output cleanup and policy scope.
Final documentation check results are recorded below after execution.

## Risks and Alternatives

Current behavior includes uncommitted work; future edits need doc refresh.
Retained ComfyUI note has stale priority/output-retention claims; kept as user
context and flagged instead of rewritten. Exact historical decisions unknown;
two ADRs are explicitly reconstructed. Python 3.10 support and real deployments
remain unverified. Timestamp-based policy reload is a suspected cause of the
reproduced test failure, not a verified diagnosis.

## Progress and Decisions

| Packet | Status | Findings / deviations |
| --- | --- | --- |
| 1 | complete | Source-driven discovery and baseline checks; failure recorded without application repair. |
| 2 | complete | Preserved `Docs/` directory convention; root AGENTS retains supplied global agreements. |
| 3 | complete | Five meaningful component docs, two retrospective ADRs, Luna packet template; narrow README contract updates. |
| 4 | complete | See final results. |

## Final Results

Created root agent instructions; six index/system guides; five component guides;
two ADRs; planning README/template, active directory marker and this completion
record. Updated README navigation and verified-current contract descriptions.
Subsystems cover filesystem transport, shared scheduling, HTTP settlement/cache,
managed backends/dashboard and alias effort policy.

Final documentation validation: all 178 local Markdown link targets in 18 Markdown
files resolve; 88 principal source symbols and referenced test names exist. Code
fences are balanced. `git diff --check` passed; separate Git no-index whitespace
checks include all 18 new files (untracked Markdown and active directory marker).
Reviewed the README diff and each new document for scope, traceability, ownership
and entering-agent retrieval. Source/test/registry/script/config and retained-note
SHA-256 hashes match the recorded baseline; the two pre-existing document deletions
remain. No application files were changed. CLI help passed; baseline pytest and
targeted reload rerun failed as described above, so the repository test suite is
not reported as passing.

Remaining issues: pre-existing reload test failure; unverified minimum-Python,
real GPU, sync and subscriber acceptance; direct test coverage gaps recorded in
[DEVELOPMENT.md](../../Docs/DEVELOPMENT.md). These do not require application code
changes to complete the documentation task.
