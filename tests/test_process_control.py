from __future__ import annotations

import sys

import pytest

from peerbridge_mcp import process_control


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object only")


class _FakeProcess:
    pid = 4242
    _handle = 99

    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    def poll(self):
        return 1 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 1


def test_job_assignment_failure_kills_exact_suspended_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Kernel:
        def CreateJobObjectW(self, *_args):
            return 123

        def SetInformationJobObject(self, *_args):
            return True

        def AssignProcessToJobObject(self, *_args):
            return False

        def CloseHandle(self, *_args):
            calls.append("close")
            return True

    monkeypatch.setattr(process_control, "_KERNEL32", Kernel())
    child = _FakeProcess()

    with pytest.raises(OSError, match="unable to bind"):
        process_control.attach_process_tree(child)

    assert child.killed is True
    assert child.waited is True
    assert calls == ["close"]


def test_job_termination_never_reopens_descendant_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    class Kernel:
        def TerminateJobObject(self, _handle, code):
            calls.append(("terminate", int(code)))
            return True

        def CloseHandle(self, _handle):
            calls.append(("close", 0))
            return True

    monkeypatch.setattr(process_control, "_KERNEL32", Kernel())
    child = _FakeProcess()
    child._peerbridge_job_handle = 321
    child.killed = True

    process_control.terminate_process_tree(child)

    assert calls == [("terminate", 1), ("close", 0)]
