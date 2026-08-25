from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_maintainer_showcase_emits_auditable_provider_free_receipt(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "examples" / "demo_workflow.py"),
            "--workspace",
            str(tmp_path / "showcase"),
            "--scope",
            "demo",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["schema"] == "peerbridge.maintainer-showcase.v1"
    assert receipt["synthetic"] is True
    assert receipt["status"] == "complete"
    assert receipt["participants"] == [
        "claude-demo",
        "codex-demo",
        "grok-demo",
        "kimi-demo",
    ]
    assert receipt["overlapping_writer_rejected"] is True
    assert receipt["review_quorum"] == 2
    assert receipt["approved_reviewers"] == ["claude-demo", "grok-demo"]
    assert receipt["artifact_sha256_before"] != receipt["artifact_sha256_after"]
    assert receipt["audit"]["valid"] is True
    assert receipt["audit"]["error_count"] == 0
    assert "lease_token" not in completed.stdout
