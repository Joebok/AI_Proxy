"""Local process witness; retains child ownership across proxy restart."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4


def process_identity(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        handle = kernel.OpenProcess(0x101000, False, pid)
        if not handle:
            return None
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return None
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                return None
            return {"pid": pid, "created": (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime}
        finally:
            kernel.CloseHandle(handle)
    try:
        return {"pid": pid, "created": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]}
    except OSError:
        return None


def process_alive(identity):
    return bool(identity and process_identity(identity["pid"]) == identity)


def save(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "-" + uuid4().hex)
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(record, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def execution_lock(path):
    path = path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    with execution_lock(args.journal):
        record = json.loads(args.journal.read_text(encoding="utf-8"))
        if record.get("phase") != "RESERVED" or not process_alive(record.get("proxy")):
            return 125
        record.update(host=process_identity(os.getpid()), phase="STARTING")
        save(args.journal, record)
    # Start suspended on Windows so no grandchild can escape containment before
    # the kill-on-close job is installed. Proxy death leaves this host running;
    # host death kills the complete worker tree.
    child = subprocess.Popen(command, shell=False, creationflags=0x4 if os.name == "nt" else 0,
                             start_new_session=os.name != "nt")
    job_handle = None
    if os.name == "nt":
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from file_proxy.backend import assign_windows_job, BackendManager
        job_handle = assign_windows_job(child)
        BackendManager._resume_windows_process(child.pid)
    record.update(child=process_identity(child.pid), phase="RUNNING")
    save(args.journal, record)
    try:
        code = child.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            import signal
            os.killpg(child.pid, signal.SIGKILL)
        else:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateJobObject(job_handle, 124)
        child.wait()
        print("Worker exceeded its execution deadline", file=sys.stderr)
        code = 124
    if job_handle is not None:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(job_handle)
    record.update(phase="EXITED", exit_code=code, host=None, child=None)
    save(args.journal, record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
