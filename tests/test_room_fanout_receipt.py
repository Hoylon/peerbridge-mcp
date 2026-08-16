from __future__ import annotations

import json
from pathlib import Path

from peerbridge_mcp.bridge import Bridge, stable_sha256
from peerbridge_mcp.openai_compatible_runner import RECEIPT_SCHEMA as OPENAI_RUN_SCHEMA
from peerbridge_mcp.room_fanout_receipt import (
    RECEIPT_SCHEMA,
    RECEIPT_SCHEMA_V1,
    capture_room_fanout_receipt,
    verify_room_fanout_receipt,
)


def _bridge(root: Path, agent: str, **identity: str) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "peerbridge.sqlite3",
        agent,
        "test-scope",
        **identity,
    )


def _capability(path: Path, provider: str, model: str) -> None:
    receipt = {
        "schema": OPENAI_RUN_SCHEMA,
        "route": {
            "provider_id": provider,
            "model_id": model,
            "response_model_id": model,
            "route_class": "relay",
            "secret_present": True,
        },
        "mcp_calls": [
            {"method": "initialize"},
            {"method": "tools/list"},
            {"method": "tools/call", "tool_name": "bridge_status"},
        ],
        "tool_calls": [{"name": "bridge_status"}],
        "raw_content_recorded": False,
        "credential_contents_recorded": False,
    }
    receipt["receipt_sha256"] = stable_sha256(receipt)
    path.write_text(json.dumps(receipt), encoding="utf-8")


def test_room_fanout_receipt_binds_two_completed_routes(tmp_path: Path) -> None:
    human = _bridge(tmp_path, "human-operator")
    alpha = _bridge(
        tmp_path,
        "alpha",
        provider_id="relay-alpha",
        model_id="model-alpha",
        route_class="relay",
    )
    beta = _bridge(
        tmp_path,
        "beta",
        provider_id="relay-beta",
        model_id="model-beta",
        route_class="relay",
    )
    for agent, provider, model in (
        ("alpha", "relay-alpha", "model-alpha"),
        ("beta", "relay-beta", "model-beta"),
    ):
        human.upsert_route_profile(
            {
                "route_id": f"route-{agent}",
                "agent_id": agent,
                "provider_id": provider,
                "model_id": model,
                "route_class": "relay",
            }
        )
    room = human.create_room({"room_id": "lab", "name": "Lab"})
    assert room["room_id"] == "lab"
    for agent in ("alpha", "beta"):
        human.join_room(
            {
                "room_id": "lab",
                "agent_id": agent,
                "route_profile_id": f"route-{agent}",
            }
        )
    human.send_room_fanout(
        {
            "room_id": "lab",
            "task_id": "fanout-task",
            "subject": "Question",
            "body": "Reply independently.",
        }
    )
    for worker in (alpha, beta):
        claim = worker.claim_message_dispatch({})
        worker.complete_message_dispatch(
            {
                "message_id": claim["message"]["message_id"],
                "lease_token": claim["lease_token"],
                "body": f"Reply from {worker.agent_id}",
                "inference_receipt_sha256": stable_sha256(worker.agent_id),
            }
        )
    capability_paths = {}
    for agent, provider, model in (
        ("alpha", "relay-alpha", "model-alpha"),
        ("beta", "relay-beta", "model-beta"),
    ):
        path = tmp_path / f"{agent}.json"
        _capability(path, provider, model)
        capability_paths[agent] = path

    receipt = capture_room_fanout_receipt(
        db_path=human.db_path,
        scope="test-scope",
        task_id="fanout-task",
        room_id="lab",
        capability_receipt_paths=capability_paths,
    )
    output = tmp_path / "fanout.json"
    output.write_text(json.dumps(receipt), encoding="utf-8")
    before = (output.read_bytes(), output.stat().st_mtime_ns)
    result = verify_room_fanout_receipt(output)

    assert result["valid"] is True
    assert result["writes_performed"] == 0
    assert receipt["recipient_agents"] == ["alpha", "beta"]
    assert receipt["claims"]["exact_dispatch_inference_receipt_artifacts_available"] is False
    assert before == (output.read_bytes(), output.stat().st_mtime_ns)
    assert "Reply independently" not in output.read_text(encoding="utf-8")


def test_room_fanout_receipt_detects_receipt_tamper(tmp_path: Path) -> None:
    output = tmp_path / "tampered.json"
    output.write_text(
        json.dumps(
            {
                "schema": RECEIPT_SCHEMA,
                "receipt_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    result = verify_room_fanout_receipt(output)
    assert result["valid"] is False
    assert "receipt_sha256" in result["errors"]


def test_room_fanout_receipt_binds_terminal_failed_seat_without_hang(
    tmp_path: Path,
) -> None:
    human = _bridge(tmp_path, "human-operator")
    alpha = _bridge(
        tmp_path,
        "alpha",
        provider_id="relay-alpha",
        model_id="model-alpha",
        route_class="relay",
    )
    beta = _bridge(
        tmp_path,
        "beta",
        provider_id="relay-beta",
        model_id="model-beta",
        route_class="relay",
    )
    for agent, provider, model in (
        ("alpha", "relay-alpha", "model-alpha"),
        ("beta", "relay-beta", "model-beta"),
    ):
        human.upsert_route_profile(
            {
                "route_id": f"route-{agent}",
                "agent_id": agent,
                "provider_id": provider,
                "model_id": model,
                "route_class": "relay",
            }
        )
    human.create_room({"room_id": "lab", "name": "Lab"})
    for agent in ("alpha", "beta"):
        human.join_room(
            {
                "room_id": "lab",
                "agent_id": agent,
                "route_profile_id": f"route-{agent}",
            }
        )
    human.send_room_fanout(
        {
            "room_id": "lab",
            "task_id": "mixed-fanout-task",
            "subject": "Question",
            "body": "Reply independently.",
        }
    )
    alpha_claim = alpha.claim_message_dispatch({})
    alpha.complete_message_dispatch(
        {
            "message_id": alpha_claim["message"]["message_id"],
            "lease_token": alpha_claim["lease_token"],
            "body": "Reply from alpha",
            "inference_receipt_sha256": stable_sha256("alpha"),
        }
    )
    beta_claim = beta.claim_message_dispatch({})
    beta.fail_message_dispatch(
        {
            "message_id": beta_claim["message"]["message_id"],
            "lease_token": beta_claim["lease_token"],
            "error_code": "configuration_invalid",
            "retryable": False,
        }
    )
    alpha_capability = tmp_path / "alpha.json"
    _capability(alpha_capability, "relay-alpha", "model-alpha")

    receipt = capture_room_fanout_receipt(
        db_path=human.db_path,
        scope="test-scope",
        task_id="mixed-fanout-task",
        room_id="lab",
        capability_receipt_paths={"alpha": alpha_capability},
    )
    output = tmp_path / "mixed-fanout.json"
    output.write_text(json.dumps(receipt), encoding="utf-8")
    before = (output.read_bytes(), output.stat().st_mtime_ns)
    result = verify_room_fanout_receipt(output)

    assert result["valid"] is True
    assert result["writes_performed"] == 0
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["completed_agents"] == ["alpha"]
    assert receipt["failed_agents"] == ["beta"]
    assert receipt["claims"]["failed_seat_did_not_hang_fanout"] is True
    assert receipt["claims"]["no_reply_cascade_observed"] is True
    assert before == (output.read_bytes(), output.stat().st_mtime_ns)


def test_room_fanout_v2_reports_partial_direct_mcp_capability_honestly(
    tmp_path: Path,
) -> None:
    human = _bridge(tmp_path, "human-operator")
    workers = []
    for agent in ("alpha", "beta"):
        worker = _bridge(
            tmp_path,
            agent,
            provider_id=f"relay-{agent}",
            model_id=f"model-{agent}",
            route_class="relay",
        )
        workers.append(worker)
        human.upsert_route_profile(
            {
                "route_id": f"route-{agent}",
                "agent_id": agent,
                "provider_id": f"relay-{agent}",
                "model_id": f"model-{agent}",
                "route_class": "relay",
            }
        )
    human.create_room({"room_id": "lab", "name": "Lab"})
    for agent in ("alpha", "beta"):
        human.join_room(
            {
                "room_id": "lab",
                "agent_id": agent,
                "route_profile_id": f"route-{agent}",
            }
        )
    human.send_room_fanout(
        {
            "room_id": "lab",
            "task_id": "partial-capability-task",
            "subject": "Question",
            "body": "Reply independently.",
        }
    )
    for worker in workers:
        claim = worker.claim_message_dispatch({})
        worker.complete_message_dispatch(
            {
                "message_id": claim["message"]["message_id"],
                "lease_token": claim["lease_token"],
                "body": f"Reply from {worker.agent_id}",
                "inference_receipt_sha256": stable_sha256(worker.agent_id),
            }
        )
    alpha_capability = tmp_path / "alpha.json"
    _capability(alpha_capability, "relay-alpha", "model-alpha")

    receipt = capture_room_fanout_receipt(
        db_path=human.db_path,
        scope="test-scope",
        task_id="partial-capability-task",
        room_id="lab",
        capability_receipt_paths={"alpha": alpha_capability},
    )
    output = tmp_path / "partial-capability.json"
    output.write_text(json.dumps(receipt), encoding="utf-8")

    assert verify_room_fanout_receipt(output)["valid"] is True
    assert receipt["mcp_capability_bound_agents"] == ["alpha"]
    assert receipt["claims"]["provider_mcp_capability_independently_bound"] is False
    assert receipt["claims"]["dispatch_inference_receipt_hashes_bound"] is True


def test_room_fanout_v1_receipt_remains_verifiable(tmp_path: Path) -> None:
    human = _bridge(tmp_path, "human-operator")
    capability_paths = {}
    workers = []
    for agent in ("alpha", "beta"):
        provider = f"relay-{agent}"
        model = f"model-{agent}"
        worker = _bridge(
            tmp_path,
            agent,
            provider_id=provider,
            model_id=model,
            route_class="relay",
        )
        workers.append(worker)
        human.upsert_route_profile(
            {
                "route_id": f"route-{agent}",
                "agent_id": agent,
                "provider_id": provider,
                "model_id": model,
                "route_class": "relay",
            }
        )
        path = tmp_path / f"{agent}.json"
        _capability(path, provider, model)
        capability_paths[agent] = path
    human.create_room({"room_id": "lab", "name": "Lab"})
    for agent in ("alpha", "beta"):
        human.join_room(
            {
                "room_id": "lab",
                "agent_id": agent,
                "route_profile_id": f"route-{agent}",
            }
        )
    human.send_room_fanout(
        {
            "room_id": "lab",
            "task_id": "v1-task",
            "subject": "Question",
            "body": "Reply independently.",
        }
    )
    for worker in workers:
        claim = worker.claim_message_dispatch({})
        worker.complete_message_dispatch(
            {
                "message_id": claim["message"]["message_id"],
                "lease_token": claim["lease_token"],
                "body": f"Reply from {worker.agent_id}",
                "inference_receipt_sha256": stable_sha256(worker.agent_id),
            }
        )
    receipt = capture_room_fanout_receipt(
        db_path=human.db_path,
        scope="test-scope",
        task_id="v1-task",
        room_id="lab",
        capability_receipt_paths=capability_paths,
        receipt_schema=RECEIPT_SCHEMA_V1,
    )
    output = tmp_path / "fanout-v1.json"
    output.write_text(json.dumps(receipt), encoding="utf-8")

    assert receipt["schema"] == RECEIPT_SCHEMA_V1
    assert verify_room_fanout_receipt(output)["valid"] is True
