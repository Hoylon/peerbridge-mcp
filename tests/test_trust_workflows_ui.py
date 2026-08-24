from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from peerbridge_mcp.localization import translate
from peerbridge_mcp.monitor import COLORS
from peerbridge_mcp.trust_workflows_ui import (
    TrustWorkflowsPage,
    proof_bundle_output_path,
    split_control_values,
)


def test_control_value_split_is_ordered_and_deduplicated() -> None:
    assert split_control_values("a, b\na; c, a") == ["a", "b", "c"]


def test_proof_output_path_is_unique_relative_and_task_bound() -> None:
    observed = datetime(2026, 8, 19, 1, 2, 3, tzinfo=timezone.utc)
    first = proof_bundle_output_path("task-one", observed)
    second = proof_bundle_output_path("task-one", observed)
    assert first.startswith(
        ".peerbridge-artifacts/proof-bundles/task-one-20260819T010203Z-"
    )
    assert first != second
    assert not Path(first).is_absolute()
    with pytest.raises(ValueError, match="invalid task id"):
        proof_bundle_output_path("../escape", observed)


def test_trust_workflows_page_has_complete_local_control_surface(
    tmp_path: Path,
) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk is unavailable: {exc}")
    root.withdraw()
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        name: str,
        arguments: dict[str, object],
        callback: Callable[[dict[str, object] | None, str | None], None],
    ) -> None:
        calls.append((name, arguments))
        callback({"status": "QUEUED"}, None)

    try:
        page = TrustWorkflowsPage(
            root,
            project_root=tmp_path,
            translate=lambda key: translate("en", key),
            colors=COLORS,
            execute_tool=execute,
        )
        snapshot = SimpleNamespace(
            operations=(),
            schedules=(),
            trust_records=(),
            permission_decisions=(),
            execution_bindings=(),
            capabilities=(),
            capability_grants=(),
            task_briefings=(),
            decision_conflicts=(),
        )
        page.render(snapshot)
        assert all(page.self_test().values())
        scan_requests: list[bool] = []
        page.set_verification_callbacks(
            status_provider=lambda: {
                "state": "running",
                "thread_alive": True,
                "scan_count": 2,
                "last_counts": {"created": 1, "open_automatic": 1},
            },
            request_scan=lambda: scan_requests.append(True),
        )
        page._scan_verification_now()
        assert scan_requests == [True]

        page.operation_task.set("Run one bounded local audit.")
        page._enqueue_operation()
        assert calls[0][0] == "enqueue_workflow"
        assert calls[0][1]["workflow_id"] == "implement-review"
        assert calls[0][1]["working_directory"] == "."
        assert calls[0][1]["max_attempts"] == 1
        assert calls[0][1]["timeout_seconds"] == 1800

        page.permission_decision.set(translate("en", "trustui.choice.deny"))
        page.operation_workflow.set(
            translate("en", "trustui.workflow.read-only-audit")
        )
        page._enqueue_operation()
        assert calls[1][0] == "enqueue_workflow"
        assert calls[1][1]["workflow_id"] == "read-only-audit"
        assert calls[1][1]["max_attempts"] == 3
        page._t = lambda key: translate("zh-Hant", key)
        page.apply_locale()
        assert page.permission_decision.get() == translate(
            "zh-Hant", "trustui.choice.deny"
        )
        assert page.operation_workflow.get() == translate(
            "zh-Hant", "trustui.workflow.read-only-audit"
        )
        assert page._choice_id(
            page.permission_decision.get(), page._permission_labels, "allow"
        ) == "deny"

        page._request_release_gate()
        assert calls[-1][0] == "request_release_gate"
        page._release_fingerprint = "a" * 64
        page.release_reason.set("The exact gate was reviewed by the operator.")
        page._decide_release_gate("approve")
        assert calls[-1] == (
            "decide_release_gate",
            {
                "fingerprint": "a" * 64,
                "decision": "approve",
                "reason": "The exact gate was reviewed by the operator.",
            },
        )
    finally:
        root.destroy()
