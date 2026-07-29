# File Job Proxy

File Job Proxy is a standalone Python 3 transport for running application-owned
workers on a designated machine. Applications subscribe by registering trusted
local worker commands, publishing self-contained job envelopes, and harvesting
completed answer envelopes. The proxy has no application imports and imposes no
application payload schema.

This document is the integration contract for an AI agent adding proxy support
to another application.

## Transport model

Treat `File_Proxy` as an **eventually consistent message transport**, not as a
filesystem API or shared application storage. Its directories are
directory-shaped message envelopes which may be replicated by Dropbox or a
similar service.

The important consequences are:

- A visible path may still have missing, stale, locked, or partially synced
  contents.
- Temporary absence does not prove that a job does not exist or has completed.
- A rename is the local publication boundary, but remote observers may see the
  renamed directory before every file has arrived.
- Published asks are immutable. Never edit a ready ask in place.
- Answers are immutable transport results. Copy their data into application
  storage before acknowledging them by removal or archival.
- Application database state, routing state, retry state, and harvest
  idempotency belong outside `File_Proxy`.
- Polling must tolerate `FileNotFoundError`, `PermissionError`, malformed JSON,
  checksum mismatch, and paths moving between scans. Retry those observations
  later rather than treating them as terminal state.
- Do not use directory listings as an authoritative state machine. A job can
  move from `Ask` to `Running` to `Answer` while another machine temporarily
  sees any subset of those states.

The proxy validates declared request files and waits up to
`--sync-grace-seconds` (300 seconds by default) for their sizes and SHA-256
digests to match. On completion it writes `proxy_result.json` with an
`output_files` inventory. A subscriber must likewise wait until that entire
inventory is locally present and valid before harvesting.

## Delivery and execution semantics

- Jobs from every subscriber share one strict FIFO queue, ordered by
  `created_at`, then `subscriber_id`, then `job_id`.
- Exactly one registered worker subprocess is launched at a time by the one
  designated proxy instance.
- There is no automatic retry. A nonzero exit, timeout, launch failure, unsafe
  output, or interrupted proxy produces a terminal answer.
- There is no proxy acknowledgement protocol. The subscriber owns harvest
  idempotency and answer retention.
- Reusing a `job_id` while its old ask or answer remains is an error. Generate a
  new ID for an intentional retry and record the relationship in
  subscriber-owned metadata.
- A crash after the worker caused an external side effect but before its answer
  was published is inherently ambiguous. Workers should be idempotent for a
  given `(subscriber_id, job_id)` or use their target system's idempotency key.
- The singleton lock is an advisory local/Dropbox safety mechanism, not a
  distributed lock. Operate exactly one proxy on one assigned worker machine.

These semantics are best described as single-attempt, eventually visible
delivery—not exactly-once processing.

## Queue layout

```text
File_Proxy/
  Ask/<subscriber_id>/<job_id>/
  Running/<subscriber_id>/<job_id>/
  Answer/<subscriber_id>/<job_id>/
  Control/proxy.lock
```

`--root` accepts either the parent queue directory or its `File_Proxy`
subdirectory; both normalize to the same queue.

The proxy owns moves between the three state directories. Subscribers may only:

1. build a hidden `Ask/<subscriber_id>/.<job_id>.staging` envelope;
2. publish it by renaming it to `Ask/<subscriber_id>/<job_id>`;
3. read their own `Answer/<subscriber_id>` envelopes; and
4. remove or archive an answer only after durable, idempotent harvesting.

Never write directly to `Running`, write `proxy_result.json`, inspect another
subscriber's answers, or rename a ready ask.

## Add a subscriber to another application

An integrating agent should implement four separate pieces:

1. **Registry entry** on the proxy host, mapping fixed worker names to trusted
   local commands.
2. **Publisher service** in the application backend, creating complete and
   immutable request envelopes.
3. **One-job worker executables** in the subscriber application. The proxy
   invokes these; they are not persistent queue consumers.
4. **Answer harvester service** in the application backend, verifying complete
   answer sync and applying results idempotently.

Keep these pieces behind application service interfaces so web routes, CLIs,
and scheduled jobs call the same behavior. Do not put queue discovery,
publication, harvesting, or retry logic directly in a UI or route handler.

### 1. Choose stable identifiers and payloads

`subscriber_id`, `job_id`, and worker names must match:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
```

Use a stable, globally unique ID such as `uuid.uuid4().hex` for every logical
attempt. Payload files are subscriber-owned. A typical request is:

```text
.<job_id>.staging/
  request.json
  inputs/
    source.bin
  job.json
```

All filesystem paths referenced anywhere in JSON must be relative to the job
envelope. Absolute paths, `..` traversal, and escaping symlinks are rejected.
JSON keys named `path`, ending in `_path`, `_file`, `_files`, or `_dir` are
treated as filesystem references, except `api_path`, `endpoint_path`, and
`url_path`. Prefer portable POSIX-style values such as `inputs/source.bin`.

Do not put machine-local destination paths in a job. Put a logical destination
or application record ID in subscriber metadata, then resolve it locally during
harvest. This is essential when the producer and worker use different machines.

### 2. Register trusted workers

Create one JSON file per subscriber in the proxy's registry directory:

```json
{
  "subscriber_id": "example_app",
  "workers": {
    "uppercase": {
      "command": [
        "python3",
        "-m",
        "example_app.workers.uppercase"
      ],
      "working_directory": "${EXAMPLE_APP_ROOT}",
      "timeout_seconds": 300
    },
    "thumbnail": {
      "command": [
        "python3",
        "-m",
        "example_app.workers.thumbnail",
        "--config",
        "${EXAMPLE_APP_ROOT}/config.toml"
      ],
      "working_directory": "${EXAMPLE_APP_ROOT}",
      "timeout_seconds": 900
    }
  }
}
```

Registry `${NAME}` values are expanded from the proxy process environment.
Commands are argument arrays and run with `shell=False`. The proxy appends:

```text
--job-dir <absolute-path-to-Running/subscriber/job>
```

Workers and their dependencies must be installed locally on the proxy host.
Never place executable code in the synchronized queue and never accept a
command from a job payload.

Restart the proxy after registry changes, then validate:

```powershell
$env:EXAMPLE_APP_ROOT = "C:\path\to\example_app"
file-proxy --root "C:\path\to\AI_Queue" --registry-dir ".\registries" validate-subscriber example_app
```

### 3. Implement atomic publication

The publisher must write payload files first, compute their inventory, write
`job.json` last, and rename the staging directory once. `job.json` does not
inventory itself.

```python
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(envelope: Path) -> list[dict[str, object]]:
    records = []
    for path in sorted(item for item in envelope.rglob("*") if item.is_file()):
        if path.name.startswith(".") or path.name == "job.json":
            continue
        records.append(
            {
                "path": path.relative_to(envelope).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def publish_uppercase(
    queue_parent: Path,
    job_id: str,
    text: str,
    application_record_id: str,
) -> None:
    subscriber_id = "example_app"
    worker = "uppercase"
    ask_root = queue_parent / "File_Proxy" / "Ask" / subscriber_id
    ask_root.mkdir(parents=True, exist_ok=True)
    staging = ask_root / f".{job_id}.staging"
    ready = ask_root / job_id

    if staging.exists() or ready.exists():
        raise FileExistsError(job_id)

    staging.mkdir()
    try:
        request = {
            "application_record_id": application_record_id,
            "input_file": "inputs/request.txt",
            "output_file": "outputs/result.txt",
        }
        (staging / "inputs").mkdir()
        (staging / "inputs" / "request.txt").write_text(text, encoding="utf-8")
        (staging / "request.json").write_text(
            json.dumps(request, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "protocol_version": 1,
            "job_id": job_id,
            "subscriber_id": subscriber_id,
            "worker": worker,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": _inventory(staging),
        }
        temporary_manifest = staging / ".job.json.tmp"
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_manifest, staging / "job.json")
        os.replace(staging, ready)
    except BaseException:
        # This only removes this unpublished, uniquely named staging envelope.
        if staging.exists():
            shutil.rmtree(staging)
        raise

```

The staging and ready directories must be on the same filesystem for atomic
rename. Do not use copy-as-publish. If publication raises after the rename, the
result is ambiguous: check for the same `job_id` later rather than publishing a
different duplicate immediately. Generate and persist the ID before publication
so the application can always reconcile that ambiguity:

```python
job_id = uuid.uuid4().hex
repository.create_pending_job(job_id, application_record_id)
publish_uppercase(queue_parent, job_id, text, application_record_id)
repository.mark_published(job_id)
```

### 4. Implement one-job workers

A worker parses `--job-dir`, validates its subscriber-owned payload, performs
one job, writes outputs beneath that directory, and exits:

```python
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _inside(job_dir: Path, relative: str) -> Path:
    candidate = (job_dir / relative).resolve()
    candidate.relative_to(job_dir.resolve())
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", required=True, type=Path)
    args = parser.parse_args()

    job_dir = args.job_dir.resolve()
    request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    source = _inside(job_dir, request["input_file"])
    destination = _inside(job_dir, request["output_file"])
    destination.parent.mkdir(parents=True, exist_ok=True)

    transformed = source.read_text(encoding="utf-8").upper()
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(transformed, encoding="utf-8")
    os.replace(temporary, destination)

    result = {
        "application_record_id": request["application_record_id"],
        "result_file": destination.relative_to(job_dir).as_posix(),
    }
    temporary_result = job_dir / ".result.json.tmp"
    temporary_result.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_result, job_dir / "result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Worker rules:

- Return `0` only after all outputs are complete and closed.
- Return nonzero and write a useful diagnostic to stderr on failure.
- Do not move, rename, or delete the job directory or modify `job.json`.
- Keep all outputs inside the job directory. Do not create escaping symlinks.
- Write each output to a hidden temporary name and atomically replace its final
  name where practical.
- Treat stdout and stderr as bounded diagnostics; both are captured into
  `proxy_result.json`, so do not emit secrets or unbounded model output.
- Make external effects idempotent by `job_id`.
- Do not implement queue polling, retries, or lifecycle transitions in a worker.

An image-style worker follows the same contract:

```python
def run_thumbnail(job_dir: Path, request: dict) -> None:
    from PIL import Image

    source = _inside(job_dir, request["input_file"])
    destination = _inside(job_dir, request["output_file"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp{destination.suffix}")
    with Image.open(source) as image:
        image.thumbnail((512, 512))
        image.save(temporary)
    os.replace(temporary, destination)
```

Only use such a worker if its dependency is installed on the worker machine.

### 5. Harvest answers safely

An answer is eligible only when:

1. `proxy_result.json` parses and its identity matches the directory;
2. every `output_files` record has a safe relative path, expected size, and
   matching SHA-256 digest; and
3. the application's idempotency ledger says the answer is not already applied.

`output_files` inventories the complete envelope other than
`proxy_result.json`, including original inputs and worker outputs. Select
subscriber outputs according to the subscriber's own `result.json`, but verify
the complete proxy inventory first.

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath


def _complete_inventory(answer: Path, records: object) -> bool:
    if not isinstance(records, list):
        return False
    root = answer.resolve()
    for record in records:
        if not isinstance(record, dict):
            return False
        relative = record.get("path")
        size = record.get("size")
        expected = record.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(expected, str)
            or len(expected) != 64
        ):
            return False
        posix = PurePosixPath(relative)
        windows = PureWindowsPath(relative)
        if (
            not relative
            or posix.is_absolute()
            or windows.is_absolute()
            or ".." in posix.parts
            or ".." in windows.parts
        ):
            return False
        path = answer.joinpath(*posix.parts)
        try:
            path.resolve().relative_to(root)
            if not path.is_file() or path.stat().st_size != size:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected.lower():
                return False
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return False
    return True


def read_ready_answer(answer: Path, subscriber_id: str) -> dict | None:
    try:
        proxy_result = json.loads(
            (answer / "proxy_result.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, PermissionError, OSError, UnicodeError, json.JSONDecodeError):
        return None

    if (
        proxy_result.get("protocol_version") != 1
        or proxy_result.get("subscriber_id") != subscriber_id
        or proxy_result.get("job_id") != answer.name
        or proxy_result.get("status") not in {"SUCCEEDED", "FAILED", "INVALID"}
        or not _complete_inventory(answer, proxy_result.get("output_files"))
    ):
        return None
    return proxy_result
```

A scheduled subscriber harvester can then poll:

```python
def harvest_once(answer_root: Path, repository, artifact_store) -> None:
    try:
        candidates = list(answer_root.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return

    for answer in candidates:
        if not answer.is_dir() or answer.name.startswith("."):
            continue
        proxy_result = read_ready_answer(answer, "example_app")
        if proxy_result is None:
            continue

        job_id = proxy_result["job_id"]
        if repository.was_harvested(job_id):
            continue

        if proxy_result["status"] == "SUCCEEDED":
            try:
                result = json.loads((answer / "result.json").read_text(encoding="utf-8"))
                source = _inside(answer, result["result_file"])
                durable_uri = artifact_store.copy_from(source, job_id)
                repository.apply_success_once(job_id, result, durable_uri)
            except (FileNotFoundError, PermissionError, OSError, ValueError, json.JSONDecodeError):
                continue
        else:
            repository.apply_failure_once(
                job_id,
                proxy_result["status"],
                proxy_result.get("error_type"),
                proxy_result.get("error_message"),
            )

        # A separate retention process may archive/remove the answer only after
        # the repository transaction above is durably committed.
```

`apply_success_once` and `apply_failure_once` should enforce a unique key on
`(subscriber_id, job_id)`. If the application cannot atomically combine its
business update with that idempotency record, design the update itself to be
repeatable. Harvest on the producer machine when results target machine-local
paths.

Do not declare a terminal failure merely because an answer is not yet complete.
Leave it for a later poll. Retention should be conservative because deletion
also replicates eventually.

## Manifest contract

Every request envelope contains `job.json`:

```json
{
  "protocol_version": 1,
  "job_id": "2bf640811f704bc3bdb12c978948bf77",
  "subscriber_id": "example_app",
  "worker": "uppercase",
  "created_at": "2026-07-28T20:15:30.123456Z",
  "files": [
    {
      "path": "inputs/request.txt",
      "size": 12,
      "sha256": "64-lowercase-or-uppercase-hex-characters"
    },
    {
      "path": "request.json",
      "size": 156,
      "sha256": "64-lowercase-or-uppercase-hex-characters"
    }
  ]
}
```

`created_at` must be an ISO-8601 timestamp with an explicit UTC offset. The
proxy accepts `Z` or `+00:00`. `files` may be empty, but all material inputs
should be declared so the worker never starts against partially synced data.

The proxy creates `proxy_result.json`:

```json
{
  "protocol_version": 1,
  "job_id": "2bf640811f704bc3bdb12c978948bf77",
  "subscriber_id": "example_app",
  "worker": "uppercase",
  "status": "SUCCEEDED",
  "started_at": "2026-07-28T20:15:32.000000Z",
  "completed_at": "2026-07-28T20:15:32.100000Z",
  "duration_seconds": 0.1,
  "exit_code": 0,
  "error_type": null,
  "error_message": null,
  "stdout": "",
  "stderr": "",
  "output_files": [
    {
      "path": "inputs/request.txt",
      "size": 12,
      "sha256": "..."
    },
    {
      "path": "outputs/result.txt",
      "size": 12,
      "sha256": "..."
    },
    {
      "path": "request.json",
      "size": 156,
      "sha256": "..."
    },
    {
      "path": "result.json",
      "size": 128,
      "sha256": "..."
    }
  ]
}
```

Terminal statuses are `SUCCEEDED`, `FAILED`, and `INVALID`. Common
`error_type` values are `WORKER_EXIT`, `WORKER_TIMEOUT`, `WORKER_LAUNCH`,
`INVALID_OUTPUT_STATE`, `PROXY_INTERRUPTED`, and `INVALID_JOB`. Consumers
should preserve unknown future error types rather than rejecting the result.

## Installation and operation

Python 3.10 or newer is required:

```powershell
python3 -m pip install -e .
python3 -m pytest
```

Run continuously:

```powershell
file-proxy --root "C:\path\to\AI_Queue" --registry-dir ".\registries" run
```

Useful commands:

```text
file-proxy --root ROOT --registry-dir REGISTRIES once
file-proxy --root ROOT --registry-dir REGISTRIES status [--subscriber ID] [--job ID]
file-proxy --root ROOT --registry-dir REGISTRIES validate-subscriber ID
```

`run` also accepts `--poll-seconds` and `--sync-grace-seconds`.

## Dropbox deployment

Keep only transport envelopes in Dropbox. Install the proxy, subscriber
applications, Python, model runtimes, and image backends locally on the
designated worker machine. Executing worker code from a synchronizing transport
would permit partial-code races and allow queue writers to change executable
code.

`registries/zet.json` is the included real subscriber example. It resolves
`${ZET_PROJECT_ROOT}` locally. `run_file_proxy.bat` uses a sibling `Zet`
checkout by default; set `ZET_PROJECT_ROOT` and `AI_QUEUE_ROOT` for another
machine layout.

Run exactly one proxy for a queue. `Control/proxy.lock` records its hostname and
will not be reclaimed by another host. Two machines started before either lock
has synchronized can still race. Assign one worker machine and remove a
foreign-host lock manually only after confirming that proxy is stopped.

For a Zet cutover, stop old producers and workers, drain or archive
`Ollama_Proxy`, then start `run_file_proxy.bat`. Do not copy old queue folders
into `File_Proxy`; their layouts and manifests are incompatible.

## Integration verification checklist

Before declaring another application integrated, verify:

- The registry validates and every command launches in its configured working
  directory on the proxy host.
- Publishing uses hidden staging, inventories every material input, writes
  `job.json` last, and performs one same-filesystem rename.
- The application persists the generated `job_id` and does not infer truth from
  a transient queue listing.
- Workers accept the appended `--job-dir`, execute one job, keep outputs inside
  the envelope, and return meaningful exit codes.
- Workers are safe when the same external operation is attempted again for the
  same `job_id`.
- Harvesting verifies `proxy_result.json` identity and the full output
  inventory before reading results.
- Harvesting handles all three terminal statuses and is idempotent.
- Incomplete or temporarily unreadable asks/answers are retried by later polls.
- Results are copied to durable application storage before answer retention.
- Only the subscriber's own answer namespace is scanned.
- Tests cover success, worker failure, timeout, malformed payload, partial sync,
  duplicate harvest, and an intentional retry using a new job ID.
