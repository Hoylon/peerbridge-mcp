from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

import peerbridge_mcp.official_integration_acceptance as acceptance


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def test_acceptance_binds_source_and_child_receipts_with_zero_write_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "PeerBridge Tests")
    (root / ".gitignore").write_text(".peerbridge/\n", encoding="utf-8")
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    receipt_root = root / ".peerbridge" / "receipts"
    receipt_root.mkdir(parents=True)
    children = {}
    for name in ("codex", "claude", "grok"):
        path = receipt_root / f"{name}.json"
        path.write_text("{}\n", encoding="utf-8")
        children[name] = path

    def fake_child(project_root: Path, path: Path, *_args) -> dict[str, object]:
        return {
            **acceptance._relative_file(project_root, path),
            "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "observed_agent": path.stem,
            "observed_version": "test",
            "observed_model": "test-model",
            "tool": "bridge_status",
            "real_inference": True,
            "zero_write_verify": True,
        }

    monkeypatch.setattr(acceptance, "_acpx_child", fake_child)
    monkeypatch.setattr(acceptance, "_claude_child", fake_child)
    output = receipt_root / "acceptance.json"
    captured = acceptance.capture_acceptance(
        project_root=root,
        codex_receipt=children["codex"],
        claude_receipt=children["claude"],
        grok_receipt=children["grok"],
        output=output,
    )
    before = (output.stat().st_size, output.stat().st_mtime_ns, hashlib.sha256(output.read_bytes()).hexdigest())

    verified = acceptance.verify_acceptance(output, root)
    after = (output.stat().st_size, output.stat().st_mtime_ns, hashlib.sha256(output.read_bytes()).hexdigest())

    assert captured["source"]["file_count"] == 2
    assert verified["valid"] is True
    assert verified["writes_performed"] == 0
    assert before == after

    (root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(
        acceptance.OfficialIntegrationAcceptanceError,
        match="source state is stale",
    ):
        acceptance.verify_acceptance(output, root)


def test_acpx_acceptance_rejects_a_self_hashed_receipt_without_trusted_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    receipt_path = root / "grok-receipt.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    receipt = {
        "route_class": "official",
        "mcp_canonical_tools_called": ["bridge_status"],
        "mcp_tool_call_count": 1,
        "response_chars": 8,
        "message_id": "message-one",
        "route_profile_id": "route-one",
        "route_profile_sha256": "a" * 64,
        "requested_provider_id": "xai-official",
        "requested_model": "grok-test",
    }
    monkeypatch.setattr(acceptance, "_load_json", lambda _path: receipt)
    monkeypatch.setattr(
        acceptance,
        "verify_acpx_inference_receipt",
        lambda _receipt: {
            "receipt_sha256": "b" * 64,
            "observed_agent_name": "grok-build",
            "observed_agent_version": "test",
            "observed_model": "grok-test",
            "route_class": "official",
            "mcp_canonical_tools_called": ["bridge_status"],
            "mcp_tool_call_count": 1,
        },
    )

    with pytest.raises(
        acceptance.OfficialIntegrationAcceptanceError,
        match="trusted dispatch database",
    ):
        acceptance._acpx_child(root, receipt_path, "grok-build")
