from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerbridge_mcp.local_alpha_soak import (
    SoakError,
    build_soak_receipt,
    verify_receipt,
    write_receipt,
)


def test_local_alpha_soak_is_bounded_and_crash_recoverable(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    receipt = build_soak_receipt(
        project_root,
        message_count=100,
        page_limit=12,
        sample_rounds=8,
        max_plateau_growth_mib=64.0,
    )

    assert all(receipt["claims"].values())
    assert receipt["results"]["message_count"] == 100
    assert receipt["results"]["page_rows_returned"] == 12
    assert receipt["results"]["crash_probe"]["passed"] is True
    path = tmp_path / "receipt.json"
    write_receipt(path, receipt)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    verified = verify_receipt(path, project_root)
    after = (path.read_bytes(), path.stat().st_mtime_ns)
    assert verified["status"] == "PASS"
    assert verified["writes_performed"] == 0
    assert after == before


def test_local_alpha_soak_verifier_rejects_tampering(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    receipt = build_soak_receipt(
        project_root,
        message_count=100,
        page_limit=10,
        sample_rounds=8,
        max_plateau_growth_mib=64.0,
    )
    receipt["claims"]["memory_plateau_passed"] = False
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SoakError, match="SHA-256 mismatch"):
        verify_receipt(path, project_root)
