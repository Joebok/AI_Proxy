# 0006 — Recover queue availability without replaying transport attempts

Status: accepted 2026-10-17 as part of Joe's cross-repository fault-tolerance plan.

## Context

The shared queue may be disconnected during admission, execution, publication, or
startup. Zet requests automatic retries, while the proxy's single-attempt and
backend-ownership contracts must remain intact.

## Decision

Keep root identity/health and execution witnesses on fixed local runtime storage.
Daemon/headless/TUI runtimes keep polling an unavailable queue; HTTP listeners
remain available. Reconcile after reconnection before admission. Missing storage
does not authorize recreating a vanished job folder or publishing invalid work.

A local worker host records process creation identity and exit state. A reserved
launch is fenced under an OS lock before recovery can classify it abandoned.
Windows workers use the existing suspended-start/kill-on-close containment helper,
including descendants. Successful local outcomes are published after reconnect;
interrupted/storage-failed outcomes carry optional retry classification. The proxy
never reruns that attempt; Zet owns a fresh transport ID and logical result binding.

Native ComfyUI submission identity and phase are locally durable before POST.
An unsubmitted PREPARED guard can be cleared without a backend mutation. Ambiguous
dispatch/acceptance retains the existing managed verified-stop/external-held policy.
Settlement evidence remains local even if writing the shared submission file fails.
Local submission metadata contains identities/phase, not the graph or input bytes.

## Consequences

Protocol-v1 readers remain compatible with optional outcome fields. The runtime
directory must be on fixed local storage, outside the queue's removable volume.
One assigned proxy per queue and subscriber-owned retries remain required. These
changes preserve the existing managed native-ComfyUI guard recovery implementation.

Six approved isolated scenarios remain the maintained suite; physical drive drops,
live provider settlement and manual external-backend recovery are not established
by that suite. See [Zet storage recovery](../../../Zet/Docs/COMPONENTS/STORAGE_RECOVERY.md) for the integration.
