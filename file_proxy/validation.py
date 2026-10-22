from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from typing import Any, Iterator

from .models import InvalidJob, JobManifest, JobNotReady


PATH_KEY_SUFFIXES = ("_file", "_path", "_dir")
NON_FILESYSTEM_PATH_KEYS = {"api_path", "endpoint_path", "url_path"}


def is_link_like(path: Path) -> bool:
    """Detect symbolic links and Windows reparse points, including junctions."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse)


def remove_link_like(path: Path) -> None:
    """Remove a link entry without traversing its target."""
    if not is_link_like(path):
        raise ValueError(f"not a link-like path: {path}")
    metadata = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    directory_attribute = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if os.name == "nt" and attributes & reparse and attributes & directory_attribute:
        path.rmdir()
    else:
        path.unlink()


def find_link_like(root: Path) -> Path | None:
    """Return the first link-like entry without traversing it."""
    if is_link_like(root):
        return root
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if is_link_like(path):
                return path
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
    return None


def build_file_inventory(job_dir: Path, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    excluded = exclude or set()
    inventory = []
    linked = find_link_like(job_dir)
    if linked is not None:
        location = "." if linked == job_dir else str(linked.relative_to(job_dir))
        raise InvalidJob(f"link or reparse point is not allowed in job folder: {location}")
    pending = [job_dir]
    files = []
    while pending:
        directory = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if is_link_like(path):
                raise InvalidJob(f"link or reparse point is not allowed in job folder: {path.relative_to(job_dir)}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
    for path in sorted(files):
        relative = path.relative_to(job_dir).as_posix()
        if relative in excluded or path.name.startswith("."):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        inventory.append({"path": relative, "size": path.stat().st_size, "sha256": digest.hexdigest()})
    return inventory


def validate_file_inventory(job_dir: Path, files: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
    for record in files:
        relative = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        if (
            not isinstance(relative, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            raise InvalidJob("file inventory entry is invalid")
        validate_relative_reference(job_dir, relative)
        path = job_dir.joinpath(*PurePosixPath(relative).parts)
        try:
            if not path.is_file() or path.stat().st_size != size:
                raise JobNotReady(f"waiting for synced file: {relative}")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise JobNotReady(f"waiting for synced file: {relative}") from exc
        if digest.hexdigest() != sha256.lower():
            raise JobNotReady(f"waiting for complete synced file: {relative}")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidJob(f"invalid JSON at {path.name}: {exc}") from exc


def _path_values(value: Any, key: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            if isinstance(child_key, str):
                yield from _path_values(child, child_key.lower())
    elif isinstance(value, list):
        for child in value:
            yield from _path_values(child, key)
    elif isinstance(value, str) and key not in NON_FILESYSTEM_PATH_KEYS and (
        key == "path" or key.endswith(PATH_KEY_SUFFIXES) or key.endswith("_files")
    ):
        yield value


def validate_relative_reference(job_dir: Path, value: str) -> None:
    if not value:
        raise InvalidJob("referenced paths cannot be blank")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or ".." in posix.parts or ".." in windows.parts:
        raise InvalidJob(f"unsafe referenced path: {value}")
    if is_link_like(job_dir):
        raise InvalidJob("job folder cannot be a link or reparse point")
    target = job_dir.joinpath(*posix.parts)
    current = job_dir
    for part in posix.parts:
        current = current / part
        if is_link_like(current):
            raise InvalidJob(f"referenced path uses a link or reparse point: {value}")
    try:
        target.resolve(strict=False).relative_to(job_dir.resolve())
    except (OSError, ValueError) as exc:
        raise InvalidJob(f"referenced path escapes job folder: {value}") from exc


def validate_job_folder(job_dir: Path, expected_subscriber: str, expected_job: str) -> JobManifest:
    if is_link_like(job_dir):
        raise InvalidJob("job folder cannot be a link or reparse point")
    try:
        manifest_data = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JobNotReady("waiting for complete synced job.json") from exc
    manifest = JobManifest.from_dict(manifest_data)
    if manifest.subscriber_id != expected_subscriber:
        raise InvalidJob("subscriber_id does not match queue folder")
    if manifest.job_id != expected_job:
        raise InvalidJob("job_id does not match queue folder")
    validate_file_inventory(job_dir, manifest.files)

    root = job_dir.resolve()
    for path in job_dir.rglob("*"):
        if is_link_like(path):
            raise InvalidJob(f"link or reparse point is not allowed in job folder: {path.relative_to(job_dir)}")
        if path.is_file() and path.suffix.lower() == ".json":
            payload = read_json(path)
            for reference in _path_values(payload):
                validate_relative_reference(job_dir, reference)
    return manifest
