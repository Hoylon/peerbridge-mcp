from __future__ import annotations

import signal

import pytest

from peerbridge_mcp import process_control


def test_posix_release_kills_the_owned_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    process = type("FinishedProcess", (), {"pid": 4242})()

    monkeypatch.setattr(process_control.sys, "platform", "linux")
    monkeypatch.setattr(process_control.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        process_control.os,
        "killpg",
        lambda pid, sig: calls.append((pid, sig)),
        raising=False,
    )

    process_control.release_process_tree(process)  # type: ignore[arg-type]

    assert calls == [(4242, 9)]
