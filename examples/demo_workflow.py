from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from peerbridge_mcp.bridge import Bridge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a synthetic four-agent PeerBridge demo.")
    parser.add_argument("--workspace", default="demo-workspace")
    parser.add_argument("--scope", default="demo")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    database = root / ".peerbridge" / "peerbridge.sqlite3"
    agents = {
        name: Bridge(root, database, name, args.scope)
        for name in ("codex-demo", "grok-demo", "deepseek-demo", "kimi-demo")
    }
    for bridge in agents.values():
        bridge.touch_presence("synthetic-demo")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    task_id = f"demo-change-{stamp}"
    target = root / "src" / "demo_state.txt"
    if not target.exists():
        target.write_text("state=initial\n", encoding="utf-8")
    before = agents["codex-demo"].hash_artifact({"path": "src/demo_state.txt"})
    claim = agents["codex-demo"].claim_task(
        {
            "task_id": task_id,
            "summary": "Synthetic multi-agent quorum demonstration",
            "write_paths": ["src/demo_state.txt"],
            "approval_mode": "quorum_required",
            "required_peers": ["grok-demo", "deepseek-demo", "kimi-demo"],
            "review_quorum": 2,
        }
    )
    target.write_text(f"state=reviewed\ntask={task_id}\n", encoding="utf-8")
    proof = agents["codex-demo"].record_proof(
        {
            "task_id": task_id,
            "lease_token": claim["lease_token"],
            "change_summary": "Updated one synthetic state file",
            "changed_paths": ["src/demo_state.txt"],
            "before_hashes": {"src/demo_state.txt": before["sha256"]},
            "tests": "synthetic content check: pass",
        }
    )
    review_ids = []
    for peer_name in ("grok-demo", "deepseek-demo"):
        request = agents["codex-demo"].request_review(
            {
                "task_id": task_id,
                "lease_token": claim["lease_token"],
                "recipient": peer_name,
                "question": "Verify the synthetic file hash and bounded proof.",
                "artifact_paths": ["src/demo_state.txt"],
            }
        )
        review = agents[peer_name].submit_review(
            {
                "request_id": request["request_id"],
                "verdict": "approved",
                "score": 95,
                "findings": f"{peer_name} independently verified the synthetic artifact.",
                "artifact_paths": ["src/demo_state.txt"],
            }
        )
        review_ids.append(review["review_id"])
    human = Bridge(root, database, "human-operator", args.scope)
    message = human.send_message(
        {
            "recipient": "*",
            "task_id": task_id,
            "subject": "SYNTHETIC DEMO",
            "body": "Two-of-three peer review quorum is ready for completion.",
        }
    )
    completed = agents["codex-demo"].complete_task(
        {"task_id": task_id, "lease_token": claim["lease_token"]}
    )
    result = {
        "task_id": task_id,
        "status": completed["status"],
        "review_quorum": completed["review"]["review_quorum"],
        "approved_reviewers": completed["review"]["approved_reviewers"],
        "proof_sha256": proof["record_sha256"],
        "message_sha256": message["content_sha256"],
        "audit": agents["codex-demo"].verify_audit_chain(),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
