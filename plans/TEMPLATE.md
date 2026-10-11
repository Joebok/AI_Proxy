# <Change title>

Status: active | completed (select one)

Owner: <agent/person>

Started: <date>; updated: <date>

Copy this template into `plans/active/`; repository paths from there use `../../`.
Read `../../AGENTS.md` and `../../Docs/INDEX.md` after copying.

## Goal

<Behavior/outcome and why it matters. Explicit scope and exclusions.>

## Current Behavior

<Authoritative module/symbol/test links; observed behavior, known failure and gaps.>

## Desired Behavior

<Expected result and observable acceptance criteria.>

## Impact Analysis

| Files/modules | Interfaces/data/consumers affected | Tests and documentation |
| --- | --- | --- |
| <bounded scope> | <dependencies/compatibility> | <existing checks/docs to update> |

## Invariants and Constraints

<Relevant contracts linked from Docs/INVARIANTS.md; platform, dependency,
ownership, user-change and compatibility constraints.>

## Implementation Steps

Repeat this packet format; each packet must be Luna-sized:

1. **Packet <id>: <one outcome>**
   - Owned paths/symbols: <bounded files/modules>.
   - Depends on: <packet IDs or prerequisite evidence>.
   - Changes: <concrete ordered actions>.
   - Preserve: <specific contracts>.
   - Checkpoint: <observable acceptance>.
   - Validation: <smallest relevant existing command/inspection; mark untested>.

## Validation

<Exact commands, isolated fixtures, inspections and acceptance checks; distinguish
suggested/unexecuted checks from observed results. Avoid real-queue side effects.>

## Risks and Alternatives

<Tradeoffs, uncertain facts, resolution steps and possible regressions.>

## Progress and Decisions

| Packet | Status | Discoveries, deviations, remaining work |
| --- | --- | --- |
| <id> | pending | <update during work> |

## Final Results

<Changed paths, acceptance outcome, verified/failed/unverified checks, documentation
updates and remaining issues. On completion move to plans/completed/ and recheck
links. Do not declare success when required validation failed.>
