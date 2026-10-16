# 0004 — Native AI providers and an independent cloud lane
Status: accepted
Date: 2026-10-16

## Context
Zet requested uniform creative AI requesting/harvesting with proxy-owned execution,
one activity view, and Codex admission that does not wait for local GPU resources.
The existing scheduler deliberately waits for expensive local completion.

## Decision
Keep the shared local slot and add a trusted `backend=codex` dispatcher with two
configurable slots. Native generic workers execute frozen Codex/Ollama/ComfyUI
requests without subscriber imports. Subscribers own prompts, compiled graphs,
domain transitions and result application. One proxy capability catalog supports
cached subscriber selectors. [Native providers](../COMPONENTS/AI_PROVIDERS.md) owns
the interface and compatibility policy.

## Rationale and consequences
Priority cannot bypass an occupied local slot. Independent dispatch avoids
unnecessary cloud latency while preserving local residency/non-preemption.
Immutable File Proxy envelopes and the one-assigned-proxy-per-queue rule remain.
Cloud attempts are never automatically replayed after ambiguous interruption.
Legacy app-worker registrations remain to drain published work; new requests use
native registrations. An unresolved native ComfyUI submission durably fences local
admission rather than assuming a timed-out backend stopped. The new isolated
scenario is explicitly approved; live backend quality and deployment are unverified.
