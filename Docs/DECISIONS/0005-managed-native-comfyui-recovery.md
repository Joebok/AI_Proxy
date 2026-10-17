# 0005 — Recover native ComfyUI admission through verified managed stop

Status: accepted
Date: 2026-10-16

## Context

An interrupted native ComfyUI attempt retained its durable guard after the E:
queue drive returned. Restart stopped the owned backend but did not reconcile the
guard; all local execution remained blocked. Joe authorized failing unrecoverable
attempts and continuing queued work rather than preserving the stalled attempt.

## Decision

At the local scheduler's admission boundary, a managed runtime with an unresolved
native submission calls the existing ownership-aware `stop_comfyui`. Only after
verified stop does it archive the guard and continue. External mode and stop or
archive failures retain the guard. No unowned backend is adopted or terminated.

## Consequences

The current ambiguous managed generation can be discarded. Its file attempt keeps
the failed/interrupted terminal outcome; recovery neither rewrites published
envelopes nor resubmits the saved workflow. A subscriber can explicitly retry it.
Subsequent queued work proceeds without manual guard deletion. Restart alone is
not evidence of a free GPU: backend stop verification remains required.

This refines the native guard in [ADR 0004](0004-native-ai-and-cloud-lane.md);
HTTP lifecycle settlement and external ownership rules remain unchanged. Local
recovery archives have no automatic retention policy. The existing isolated E2E
suite covers surrounding scheduler/backend flows, but does not directly exercise
native guard recovery; no additional test scenario was authorized.
