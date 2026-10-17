# Storage outage recovery
Status: implementation complete; isolated validation complete, 2026-10-17.
Cross-repository specification and packets:
[Zet implementation plan](../../../Zet/plans/completed/2026-10-17-storage-recovery.md).

WP1: local health journal/identity and recovery leases.
WP2: resilient filesystem startup, polling, and reconnect reconciliation.
WP3: local worker/submission evidence and retryable interrupted outcomes.
WP9: status/documentation and approved six-scenario isolated suite.

Maintain immutable envelopes and one execution per transport attempt; Zet owns
fresh attempts. Keep unrelated HTTP work available. Preserve the existing
uncommitted managed-ComfyUI recovery implementation. Never detach live E: or
invoke live inference for checks. No permanent test expansion.

WP1, WP2, WP3 and WP9 are complete. See the linked implementation record for
cross-repository packet progress and validation boundaries, and
[ADR 0006](../../Docs/DECISIONS/0006-storage-outage-recovery.md) for ownership.

Final `.venv/Scripts/python.exe -m pytest -q`: **6 passed, 1 deselected in
13.14s**. Python compilation and `git diff --check` passed. Zet's four approved
scenarios also passed. Disposable controlled-worker checks covered retained
results, surviving worker ownership after proxy restart, queue absence/reconnect,
independent cloud scheduling and offline TUI counts. Windows process identity
checks now reject exited processes even while another process retains a handle.
Successful execution evidence remains local until publication succeeds.

The managed-ComfyUI changes already present were preserved. No live backend,
production queue, physical E: disconnect, deployment or production restart was
used. No maintained scenarios or dependencies were added. External backend
ambiguity still requires verified settlement before its execution lane resumes.
