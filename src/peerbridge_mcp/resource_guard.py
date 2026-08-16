"""Dependency-free local resource guards for provider runtimes.

The guard never terminates another process.  It prevents PeerBridge from
starting additional model work when the machine is already under memory
pressure and limits concurrent runtimes with crash-released file locks.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MIB = 1024 * 1024
DEFAULT_MIN_AVAILABLE_MIB = 2048
DEFAULT_MIN_AVAILABLE_FRACTION = 0.08
DEFAULT_MAX_CONCURRENT_RUNTIMES = 2


class ResourceGuardError(RuntimeError):
    """A resource gate rejected new work without exposing private data."""


class MemoryPressureError(ResourceGuardError):
    """The host does not have enough available physical memory."""


class RuntimeCapacityError(ResourceGuardError):
    """Every bounded provider-runtime slot is currently occupied."""


@dataclass(frozen=True)
class MemorySnapshot:
    total_physical_bytes: int
    available_physical_bytes: int
    process_working_set_bytes: int = 0
    process_private_bytes: int = 0

    @property
    def available_fraction(self) -> float:
        if self.total_physical_bytes <= 0:
            return 1.0
        return self.available_physical_bytes / self.total_physical_bytes


@dataclass(frozen=True)
class ResourcePolicy:
    min_available_bytes: int
    min_available_fraction: float
    max_concurrent_runtimes: int

    @classmethod
    def from_environment(cls) -> "ResourcePolicy":
        min_mib = _bounded_int_env(
            "PEERBRIDGE_MIN_AVAILABLE_MIB",
            DEFAULT_MIN_AVAILABLE_MIB,
            minimum=128,
            maximum=65536,
        )
        fraction_percent = _bounded_int_env(
            "PEERBRIDGE_MIN_AVAILABLE_PERCENT",
            int(DEFAULT_MIN_AVAILABLE_FRACTION * 100),
            minimum=1,
            maximum=50,
        )
        concurrency = _bounded_int_env(
            "PEERBRIDGE_MAX_RUNTIME_CONCURRENCY",
            DEFAULT_MAX_CONCURRENT_RUNTIMES,
            minimum=1,
            maximum=16,
        )
        return cls(
            min_available_bytes=min_mib * MIB,
            min_available_fraction=fraction_percent / 100.0,
            max_concurrent_runtimes=concurrency,
        )


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ResourceGuardError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ResourceGuardError(f"{name} is outside the supported range")
    return value


def _windows_memory_snapshot() -> MemorySnapshot:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MEMORYSTATUSEX),)
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_bool
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")

    working_set = 0
    private_bytes = 0
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            ctypes.c_ulong,
        )
        psapi.GetProcessMemoryInfo.restype = ctypes.c_bool
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            working_set = int(counters.WorkingSetSize)
            private_bytes = int(counters.PrivateUsage)
    except (AttributeError, OSError):
        pass

    return MemorySnapshot(
        total_physical_bytes=int(status.ullTotalPhys),
        available_physical_bytes=int(status.ullAvailPhys),
        process_working_set_bytes=working_set,
        process_private_bytes=private_bytes,
    )


def _posix_memory_snapshot() -> MemorySnapshot:
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
    available = int(os.sysconf("SC_AVPHYS_PAGES")) * page_size
    return MemorySnapshot(total_physical_bytes=total, available_physical_bytes=available)


def memory_snapshot() -> MemorySnapshot:
    if os.name == "nt":
        return _windows_memory_snapshot()
    try:
        return _posix_memory_snapshot()
    except (AttributeError, OSError, ValueError):
        # Unknown platforms fail open for probing only; the bounded runtime slots
        # still apply.  Returning zero total avoids a fabricated pressure signal.
        return MemorySnapshot(0, 0)


def require_memory_headroom(
    *,
    snapshot: MemorySnapshot | None = None,
    policy: ResourcePolicy | None = None,
) -> MemorySnapshot:
    observed = snapshot or memory_snapshot()
    selected = policy or ResourcePolicy.from_environment()
    if observed.total_physical_bytes <= 0:
        return observed
    required = max(
        selected.min_available_bytes,
        int(observed.total_physical_bytes * selected.min_available_fraction),
    )
    if observed.available_physical_bytes < required:
        raise MemoryPressureError(
            "host memory pressure is too high to start another PeerBridge runtime"
        )
    return observed


class _FileSlot:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextlib.contextmanager
def provider_runtime_slot(
    *,
    policy: ResourcePolicy | None = None,
    timeout: float = 0.0,
    poll_interval: float = 0.1,
    slot_root: Path | None = None,
) -> Iterator[MemorySnapshot]:
    """Reserve one crash-released cross-process provider-runtime slot."""
    selected = policy or ResourcePolicy.from_environment()
    observed = require_memory_headroom(policy=selected)
    root = slot_root or Path(tempfile.gettempdir()) / "peerbridge-runtime-slots-v1"
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired: _FileSlot | None = None
    while acquired is None:
        for index in range(selected.max_concurrent_runtimes):
            candidate = _FileSlot(root / f"provider-{index}.lock")
            if candidate.try_acquire():
                acquired = candidate
                break
        if acquired is not None or time.monotonic() >= deadline:
            break
        time.sleep(max(0.01, min(float(poll_interval), 1.0)))
        observed = require_memory_headroom(policy=selected)
    if acquired is None:
        raise RuntimeCapacityError(
            "all bounded PeerBridge provider-runtime slots are occupied"
        )
    try:
        yield observed
    finally:
        acquired.release()


__all__ = [
    "MemoryPressureError",
    "MemorySnapshot",
    "ResourceGuardError",
    "ResourcePolicy",
    "RuntimeCapacityError",
    "memory_snapshot",
    "provider_runtime_slot",
    "require_memory_headroom",
]
