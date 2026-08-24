"""Cross-platform ownership and termination for short-lived child runtimes."""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import threading
from typing import Any


_JOB_HANDLE_ATTRIBUTE = "_peerbridge_job_handle"


if sys.platform == "win32":
    from ctypes import wintypes

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _NTDLL = ctypes.WinDLL("ntdll", use_last_error=True)
    _NTDLL.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _NTDLL.NtResumeProcess.restype = ctypes.c_long

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _CREATE_SUSPENDED = 0x00000004


def _kill_exact_child(process: subprocess.Popen[Any]) -> None:
    """Terminate and reap only the process represented by this Popen handle."""

    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=2)


def process_group_popen_kwargs() -> dict[str, Any]:
    """Start a child in a process group PeerBridge can terminate as one unit."""
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | _CREATE_SUSPENDED
        }
    return {"start_new_session": True}


def attach_process_tree(process: subprocess.Popen[Any]) -> bool:
    """Bind a Windows child tree to a kill-on-close Job Object or fail closed."""
    if sys.platform != "win32":
        return True
    if not hasattr(process, "_handle"):
        _kill_exact_child(process)
        raise OSError("owned child process has no Windows process handle")
    job = _KERNEL32.CreateJobObjectW(None, None)
    if not job:
        _kill_exact_child(process)
        raise OSError("unable to create an owned Windows Job Object")
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    configured = _KERNEL32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = bool(configured) and bool(
        _KERNEL32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle))
    )
    if not assigned:
        _KERNEL32.CloseHandle(job)
        _kill_exact_child(process)
        raise OSError("unable to bind owned child to a Windows Job Object")
    setattr(process, _JOB_HANDLE_ATTRIBUTE, int(job))
    resumed = _NTDLL.NtResumeProcess(wintypes.HANDLE(process._handle))
    if resumed < 0:
        _KERNEL32.TerminateJobObject(job, 1)
        release_process_tree(process)
        _kill_exact_child(process)
        raise OSError("unable to resume owned child process")
    return True


def release_process_tree(process: subprocess.Popen[Any]) -> None:
    """Release ownership; kill-on-close also removes leaked descendants."""
    if sys.platform != "win32":
        # Every POSIX child is started as a fresh session leader. The parent may
        # exit while a helper remains in that process group, so explicitly kill
        # the now-orphaned group before releasing ownership.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        return
    handle = getattr(process, _JOB_HANDLE_ATTRIBUTE, None)
    if handle:
        setattr(process, _JOB_HANDLE_ATTRIBUTE, None)
        _KERNEL32.CloseHandle(wintypes.HANDLE(handle))


def terminate_process_tree(
    process: subprocess.Popen[Any], *, wait_seconds: float = 5.0
) -> None:
    """Terminate the exact owned child and descendants, then reap the parent."""
    wait_seconds = max(0.1, min(float(wait_seconds), 30.0))
    if sys.platform == "win32":
        handle = getattr(process, _JOB_HANDLE_ATTRIBUTE, None)
        if handle:
            with contextlib.suppress(OSError):
                _KERNEL32.TerminateJobObject(wintypes.HANDLE(handle), 1)
            release_process_tree(process)
        else:
            # attach_process_tree() fails closed, so this branch is only for a
            # caller-owned Popen that never had descendants delegated to us.
            _kill_exact_child(process)
    else:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=wait_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=wait_seconds)


def write_process_stdin_bounded(
    process: subprocess.Popen[bytes],
    payload: bytes,
    *,
    close_after: bool = False,
    timeout_seconds: float = 5.0,
) -> None:
    """Deliver stdin without letting a blocked child freeze its owner."""

    stream = process.stdin
    if stream is None or stream.closed:
        raise RuntimeError("process input stream is unavailable")
    completed = threading.Event()
    failures: list[BaseException] = []

    def write() -> None:
        try:
            stream.write(payload)
            stream.flush()
            if close_after:
                stream.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    writer = threading.Thread(
        target=write,
        name="peerbridge-bounded-stdin-writer",
        daemon=True,
    )
    writer.start()
    if not completed.wait(max(0.1, float(timeout_seconds))):
        terminate_process_tree(process, wait_seconds=2)
        completed.wait(2)
        raise TimeoutError("process input delivery exceeded the bounded deadline")
    writer.join(timeout=0.1)
    if failures:
        raise RuntimeError("process input delivery failed") from failures[0]


__all__ = [
    "attach_process_tree",
    "process_group_popen_kwargs",
    "release_process_tree",
    "terminate_process_tree",
    "write_process_stdin_bounded",
]
