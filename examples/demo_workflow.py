from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from peerbridge_mcp.bridge import Bridge, BridgeError


DEMO_AGENT_SPECS = {
    "codex-demo": {
        "client_name": "codex-app-server-contract",
        "provider_id": "synthetic-openai",
        "model_id": "fixture-codex",
    },
    "claude-demo": {
        "client_name": "claude-stream-json-contract",
        "provider_id": "synthetic-anthropic",
        "model_id": "fixture-claude",
    },
    "grok-demo": {
        "client_name": "grok-acp-contract",
        "provider_id": "synthetic-xai",
        "model_id": "fixture-grok",
    },
    "kimi-demo": {
        "client_name": "kimi-acp-contract",
        "provider_id": "synthetic-moonshot",
        "model_id": "fixture-kimi",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the synthetic PeerBridge maintainer showcase."
    )
    parser.add_argument("--workspace", default="demo-workspace")
    parser.add_argument("--scope", default="demo")
    return parser.parse_args()


def run_demo(root: Path, scope: str) -> dict[str, object]:
    """Run a provider-free maintainer workflow and return its public receipt."""

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    database = root / ".peerbridge" / "peerbridge.sqlite3"
    agents = {
        name: Bridge(root, database, name, scope, route_class="local", **identity)
        for name, identity in DEMO_AGENT_SPECS.items()
    }
    human = Bridge(root, database, "human-operator", scope)
    for name, bridge in agents.items():
        bridge.touch_presence("synthetic-maintainer-showcase")
        route_id = f"{name}-synthetic-contract"
        bridge.upsert_route_profile(
            {
                "route_id": route_id,
                "agent_id": name,
                "client_name": DEMO_AGENT_SPECS[name]["client_name"],
                "provider_id": DEMO_AGENT_SPECS[name]["provider_id"],
                "model_id": DEMO_AGENT_SPECS[name]["model_id"],
                "reasoning_mode": "fixture",
                "route_class": "local",
            }
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    task_id = f"maintainer-showcase-{stamp}"
    room_id = f"oss-showcase-{stamp.lower()}"
    human.create_room({"room_id": room_id, "name": "OSS maintainer release review"})
    for name in agents:
        human.join_room(
            {
                "room_id": room_id,
                "agent_id": name,
                "route_profile_id": f"{name}-synthetic-contract",
            }
        )

    target = root / "src" / "demo_state.txt"
    if not target.exists():
        target.write_text("state=initial\n", encoding="utf-8")
    before = agents["codex-demo"].hash_artifact({"path": "src/demo_state.txt"})
    claim = agents["codex-demo"].claim_task(
        {
            "task_id": task_id,
            "summary": "Synthetic OSS maintainer implementation and release review",
            "write_paths": ["src/demo_state.txt"],
            "approval_mode": "quorum_required",
            "required_peers": ["claude-demo", "grok-demo", "kimi-demo"],
            "review_quorum": 2,
        }
    )

    conflict_rejected = False
    conflict_reason = ""
    try:
        agents["claude-demo"].claim_task(
            {
                "task_id": f"conflicting-writer-{stamp}",
                "summary": "A second writer must not acquire the same path",
                "write_paths": ["src/demo_state.txt"],
                "approval_mode": "solo_allowed",
            }
        )
    except BridgeError as exc:
        conflict_rejected = True
        conflict_reason = str(exc)
    if not conflict_rejected:
        raise RuntimeError("overlapping writer lease was not rejected")

    target.write_text(f"state=reviewed\ntask={task_id}\n", encoding="utf-8")
    after = agents["codex-demo"].hash_artifact({"path": "src/demo_state.txt"})
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
    for peer_name in ("claude-demo", "grok-demo"):
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
    message = human.send_message(
        {
            "room_id": room_id,
            "recipient": "*",
            "task_id": task_id,
            "subject": "SYNTHETIC MAINTAINER SHOWCASE",
            "body": "Two independent reviews are bound to the exact synthetic artifact.",
        }
    )
    completed = agents["codex-demo"].complete_task(
        {"task_id": task_id, "lease_token": claim["lease_token"]}
    )
    audit = agents["codex-demo"].verify_audit_chain()
    return {
        "schema": "peerbridge.maintainer-showcase.v1",
        "synthetic": True,
        "task_id": task_id,
        "room_id": room_id,
        "status": completed["status"],
        "participants": sorted(agents),
        "adapter_contracts": {
            name: identity["client_name"]
            for name, identity in sorted(DEMO_AGENT_SPECS.items())
        },
        "overlapping_writer_rejected": conflict_rejected,
        "overlapping_writer_reason": conflict_reason,
        "review_quorum": completed["review"]["review_quorum"],
        "approved_reviewers": completed["review"]["approved_reviewers"],
        "review_ids": review_ids,
        "artifact_sha256_before": before["sha256"],
        "artifact_sha256_after": after["sha256"],
        "proof_sha256": proof["record_sha256"],
        "message_sha256": message["content_sha256"],
        "audit": audit,
    }


def main() -> int:
    args = parse_args()
    result = run_demo(Path(args.workspace), args.scope)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
