from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from peerbridge_mcp.attachments import stage_chat_attachments
from peerbridge_mcp.bridge import DEFAULT_ROOM_ID, Bridge, BridgeError, stable_sha256
from peerbridge_mcp.codex_catalog import CodexModel, CodexModelCatalog
from peerbridge_mcp.monitor import (
    BridgeReader,
    INFERENCE_ONLY,
    MCP_TOOL_LOOP,
    PixelMonitor,
    USAGE_TABLE_COLUMNS,
    active_room_recipient_ids,
    agent_route_options,
    ccswitch_route_specs,
    codex_catalog_route_options,
    discovered_route_profile_id,
    exact_route_profile,
    merge_agent_route_options,
    merge_global_agent_catalog,
    point_in_rectangle,
    provider_display_label,
    room_agent_cards,
    room_members_missing_routes,
)
from peerbridge_mcp.ccswitch import CcSwitchProvider
from peerbridge_mcp.credentials import credential_target
from tests._image_fixtures import PNG
from peerbridge_mcp.server import handle_request


def test_usage_table_columns_are_one_shared_closed_definition() -> None:
    assert USAGE_TABLE_COLUMNS == (
        "provider",
        "model",
        "calls",
        "reported",
        "input",
        "output",
        "total",
    )


def _test_credential(*parts: str) -> str:
    return "-".join(parts)


def make_bridge(
    root: Path,
    agent: str,
    *,
    session: str | None = None,
    client_name: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    reasoning_mode: str | None = None,
    route_class: str | None = None,
    discussion_coordinator: bool = False,
) -> Bridge:
    return Bridge(
        root,
        root / ".peerbridge" / "peerbridge.sqlite3",
        agent,
        "test-scope",
        session_id=session,
        client_name=client_name,
        provider_id=provider_id,
        model_id=model_id,
        reasoning_mode=reasoning_mode,
        route_class=route_class,
        discussion_coordinator=discussion_coordinator,
    )


def test_message_cursors_are_per_consumer_and_contiguous(tmp_path: Path) -> None:
    sender = make_bridge(tmp_path, "sender")
    bob = make_bridge(tmp_path, "bob")
    carol = make_bridge(tmp_path, "carol")

    first = sender.send_message(
        {"recipient": "bob", "task_id": "mail", "subject": "one", "body": "direct"}
    )
    second = sender.send_message(
        {"recipient": "*", "task_id": "mail", "subject": "two", "body": "broadcast"}
    )

    assert [m["message_id"] for m in bob.poll_messages({})["messages"]] == [
        first["message_id"],
        second["message_id"],
    ]
    out_of_order = bob.ack_message({"message_id": second["message_id"]})
    assert out_of_order["cursor"] == 0
    contiguous = bob.ack_message({"message_id": first["message_id"]})
    assert contiguous["cursor"] == second["sequence"]
    assert bob.poll_messages({})["messages"] == []

    carol_poll = carol.poll_messages({})
    assert [m["message_id"] for m in carol_poll["messages"]] == [second["message_id"]]
    assert carol.ack_message({"message_id": second["message_id"]})["cursor"] == second["sequence"]


def test_mail_and_review_consumers_cannot_impersonate_another_agent(tmp_path: Path) -> None:
    sender = make_bridge(tmp_path, "sender")
    bob = make_bridge(tmp_path, "bob")
    sent = sender.send_message(
        {"recipient": "bob", "task_id": "identity", "subject": "private", "body": "bound"}
    )

    with pytest.raises(BridgeError, match="current runtime identity"):
        bob.poll_messages({"agent_id": "sender"})
    with pytest.raises(BridgeError, match="current runtime identity"):
        bob.ack_message({"message_id": sent["message_id"], "agent_id": "sender"})
    with pytest.raises(BridgeError, match="current runtime identity"):
        bob.poll_reviews({"agent_id": "sender"})

    assert bob.poll_messages({"agent_id": "bob"})["count"] == 1


def test_message_dispatch_is_atomic_idempotent_and_secret_free(tmp_path: Path) -> None:
    sender = make_bridge(tmp_path, "sender")
    worker = make_bridge(
        tmp_path,
        "worker",
        session="worker-session",
        provider_id="relay-main",
        model_id="model-a",
        reasoning_mode="high",
        route_class="relay",
    )
    message = sender.send_message(
        {
            "recipient": "worker",
            "task_id": "dispatch",
            "subject": "Question",
            "body": "Reply once.",
            "requested_provider_id": "relay-main",
            "requested_model_id": "model-a",
            "requested_reasoning_mode": "high",
            "requested_route_class": "relay",
        }
    )
    assert message["route_request"]["route_profile_id"] is None
    assert "route_profile_sha256" not in message["route_request"]

    claim = worker.claim_message_dispatch({"lease_seconds": 60})
    assert claim["claimed"] is True
    assert claim["message"]["message_id"] == message["message_id"]
    assert claim["message"]["route_profile_sha256"] is None
    assert "route_profile_sha256" not in claim["message"]["route_request"]
    assert "lease_token" not in json.dumps(claim["dispatch"])
    assert worker.claim_message_dispatch({})["claimed"] is False

    receipt_sha = "a" * 64
    complete = worker.complete_message_dispatch(
        {
            "message_id": message["message_id"],
            "lease_token": claim["lease_token"],
            "body": "One durable answer.",
            "inference_receipt_sha256": receipt_sha,
        }
    )
    replay = worker.complete_message_dispatch(
        {
            "message_id": message["message_id"],
            "lease_token": claim["lease_token"],
            "body": "This body must not be written.",
            "inference_receipt_sha256": receipt_sha,
        }
    )
    assert complete["completed"] is True
    assert replay["idempotent_replay"] is True
    assert replay["reply_message_id"] == complete["reply_message_id"]

    with sqlite3.connect(worker.db_path) as connection:
        connection.row_factory = sqlite3.Row
        replies = connection.execute(
            "SELECT * FROM messages WHERE reply_to=?", (message["message_id"],)
        ).fetchall()
        stored = connection.execute(
            "SELECT * FROM message_dispatches WHERE message_id=?",
            (message["message_id"],),
        ).fetchone()
        usage = connection.execute(
            "SELECT usage_status, input_tokens, total_tokens, "
            "inference_receipt_sha256 FROM inference_usage WHERE message_id=?",
            (message["message_id"],),
        ).fetchone()
        event_payloads = [
            str(row[0])
            for row in connection.execute("SELECT payload_json FROM events").fetchall()
        ]
    assert len(replies) == 1
    assert replies[0]["body"] == "One durable answer."
    assert stored["status"] == "completed"
    assert stored["inference_receipt_sha256"] == receipt_sha
    assert tuple(usage) == ("unavailable", None, None, receipt_sha)
    assert claim["lease_token"] not in "".join(event_payloads)
    assert worker.verify_audit_chain()["valid"] is True


def test_message_dispatch_reclaims_expired_lease_and_rejects_old_session(
    tmp_path: Path,
) -> None:
    sender = make_bridge(tmp_path, "sender")
    first = make_bridge(tmp_path, "worker", session="first")
    message = sender.send_message(
        {"recipient": "worker", "task_id": "reclaim", "subject": "S", "body": "B"}
    )
    old_claim = first.claim_message_dispatch({"message_id": message["message_id"]})
    with sqlite3.connect(first.db_path) as connection:
        connection.execute(
            "UPDATE message_dispatches SET lease_expires_epoch=0 WHERE message_id=?",
            (message["message_id"],),
        )

    second = make_bridge(tmp_path, "worker", session="second")
    new_claim = second.claim_message_dispatch({"message_id": message["message_id"]})
    assert new_claim["claimed"] is True
    assert new_claim["dispatch"]["attempt_count"] == 2
    with pytest.raises(BridgeError, match="another session"):
        first.complete_message_dispatch(
            {
                "message_id": message["message_id"],
                "lease_token": old_claim["lease_token"],
                "body": "stale",
                "inference_receipt_sha256": "b" * 64,
            }
        )


def test_message_dispatch_renewal_extends_only_the_active_session_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr("peerbridge_mcp.bridge.time.time", lambda: clock[0])
    sender = make_bridge(tmp_path, "sender")
    worker = make_bridge(tmp_path, "worker", session="active-session")
    message = sender.send_message(
        {"recipient": "worker", "task_id": "renew", "subject": "S", "body": "B"}
    )
    claim = worker.claim_message_dispatch(
        {"message_id": message["message_id"], "lease_seconds": 60}
    )
    assert claim["dispatch"]["lease_expires_epoch"] == 1_060.0

    clock[0] = 1_050.0
    renewed = worker.renew_message_dispatch(
        {
            "message_id": message["message_id"],
            "lease_token": claim["lease_token"],
            "lease_seconds": 60,
        }
    )
    assert renewed["dispatch"]["lease_expires_epoch"] == 1_110.0
    assert "lease_token" not in json.dumps(renewed)
    with pytest.raises(BridgeError, match="token does not match"):
        worker.renew_message_dispatch(
            {
                "message_id": message["message_id"],
                "lease_token": _test_credential("test", "wrong", "token"),
                "lease_seconds": 60,
            }
        )

    clock[0] = 1_061.0
    replacement = make_bridge(tmp_path, "worker", session="replacement-session")
    assert replacement.claim_message_dispatch({"message_id": message["message_id"]})[
        "claimed"
    ] is False
    with sqlite3.connect(worker.db_path) as connection:
        payloads = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT payload_json FROM events WHERE event_type='message.dispatch_renewed'"
            )
        )
    assert claim["lease_token"] not in payloads


def test_message_dispatch_failure_can_retry_or_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr("peerbridge_mcp.bridge.time.time", lambda: clock[0])
    sender = make_bridge(tmp_path, "sender")
    worker = make_bridge(tmp_path, "worker")
    message = sender.send_message(
        {"recipient": "worker", "task_id": "failure", "subject": "S", "body": "B"}
    )
    first = worker.claim_message_dispatch({"message_id": message["message_id"]})
    failed = worker.fail_message_dispatch(
        {
            "message_id": message["message_id"],
            "lease_token": first["lease_token"],
            "error_code": "provider_temporarily_unavailable",
            "retryable": True,
        }
    )
    assert failed["dispatch"]["status"] == "retryable"
    assert failed["retry_schedule"]["not_before_epoch"] == 1_015.0
    assert (
        worker.claim_message_dispatch({"message_id": message["message_id"]})[
            "claimed"
        ]
        is False
    )
    clock[0] = 1_016.0
    second = worker.claim_message_dispatch({"message_id": message["message_id"]})
    assert second["dispatch"]["attempt_count"] == 2
    terminal = worker.fail_message_dispatch(
        {
            "message_id": message["message_id"],
            "lease_token": second["lease_token"],
            "error_code": "unsupported_provider_route",
            "retryable": False,
        }
    )
    assert terminal["dispatch"]["status"] == "failed"
    assert worker.claim_message_dispatch({"message_id": message["message_id"]})["claimed"] is False


def test_message_dispatch_retry_schedule_tamper_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [2_000.0]
    monkeypatch.setattr("peerbridge_mcp.bridge.time.time", lambda: clock[0])
    sender = make_bridge(tmp_path, "sender")
    worker = make_bridge(tmp_path, "worker")
    message = sender.send_message(
        {"recipient": "worker", "task_id": "retry-tamper", "subject": "S", "body": "B"}
    )
    claim = worker.claim_message_dispatch({"message_id": message["message_id"]})
    worker.fail_message_dispatch(
        {
            "message_id": message["message_id"],
            "lease_token": claim["lease_token"],
            "error_code": "provider_temporarily_unavailable",
            "retryable": True,
        }
    )
    with sqlite3.connect(worker.db_path) as connection:
        connection.execute(
            "UPDATE message_dispatch_retry_schedules SET not_before_epoch=0 "
            "WHERE message_id=?",
            (message["message_id"],),
        )
    clock[0] = 3_000.0
    with pytest.raises(BridgeError, match="retry schedule SHA mismatch"):
        worker.claim_message_dispatch({"message_id": message["message_id"]})


def test_rooms_reuse_global_agents_but_isolate_membership_sessions_and_cursors(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    alpha = human.create_room({"room_id": "alpha", "name": "Alpha Room"})
    beta = human.create_room({"room_id": "beta", "name": "Beta Room"})
    assert alpha["creator_joined"] and beta["creator_joined"]

    human.upsert_route_profile(
        {
            "route_id": "grok-high",
            "agent_id": "grok-relay",
            "provider_id": "relay-grok",
            "model_id": "grok",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    alpha_member = human.join_room(
        {
            "room_id": "alpha",
            "agent_id": "grok-relay",
            "route_profile_id": "grok-high",
        }
    )
    beta_member = human.join_room(
        {
            "room_id": "beta",
            "agent_id": "grok-relay",
            "route_profile_id": "grok-high",
        }
    )
    assert alpha_member["room_session_id"] != beta_member["room_session_id"]
    catalog = {item["agent_id"]: item for item in human.list_agents({})["agents"]}
    assert catalog["grok-relay"]["active_room_ids"] == ["alpha", "beta"]
    assert catalog["grok-relay"]["online"] is False
    assert catalog["grok-relay"]["route_profiles"][0]["route_id"] == "grok-high"

    first = human.send_message(
        {
            "room_id": "alpha",
            "recipient": "grok-relay",
            "task_id": "alpha-task",
            "subject": "alpha only",
            "body": "Do not leak this into beta.",
            "route_profile_id": "grok-high",
        }
    )
    second = human.send_message(
        {
            "room_id": "beta",
            "recipient": "grok-relay",
            "task_id": "beta-task",
            "subject": "beta only",
            "body": "This is a separate context.",
            "route_profile_id": "grok-high",
        }
    )
    grok = make_bridge(
        tmp_path,
        "grok-relay",
        provider_id="relay-grok",
        model_id="grok",
        reasoning_mode="high",
        route_class="relay",
    )
    assert [item["message_id"] for item in grok.poll_messages({"room_id": "alpha"})["messages"]] == [
        first["message_id"]
    ]
    assert [item["message_id"] for item in grok.poll_messages({"room_id": "beta"})["messages"]] == [
        second["message_id"]
    ]
    alpha_ack = grok.ack_message({"message_id": first["message_id"]})
    assert alpha_ack["room_id"] == "alpha"
    assert grok.poll_messages({"room_id": "alpha"})["messages"] == []
    assert grok.poll_messages({"room_id": "beta"})["count"] == 1

    left = human.leave_room({"room_id": "alpha", "agent_id": "grok-relay"})
    assert left["status"] == "left"
    assert human.room_members({"room_id": "alpha"})["count"] == 1
    assert human.room_members({"room_id": "beta"})["count"] == 2
    after_leave = {
        item["agent_id"]: item for item in human.list_agents({})["agents"]
    }
    assert after_leave["grok-relay"]["active_room_ids"] == ["beta"]
    with pytest.raises(BridgeError, match="not an active member"):
        grok.poll_messages({"room_id": "alpha"})
    with pytest.raises(BridgeError, match="not an active member"):
        human.send_message(
            {
                "room_id": "alpha",
                "recipient": "grok-relay",
                "task_id": "after-leave",
                "subject": "no delivery",
                "body": "This must fail closed.",
            }
        )

    rejoined = human.join_room(
        {
            "room_id": "alpha",
            "agent_id": "grok-relay",
            "route_profile_id": "grok-high",
        }
    )
    assert rejoined["room_session_id"] != alpha_member["room_session_id"]
    assert human.room_members({"room_id": "alpha"})["count"] == 2

    rooms = {item["room_id"]: item for item in human.list_rooms({})["rooms"]}
    assert DEFAULT_ROOM_ID in rooms
    assert rooms["alpha"]["message_count"] == 1
    assert rooms["beta"]["active_member_count"] == 2


def test_room_seat_route_persists_across_bridge_restart_and_room_switch(
    tmp_path: Path,
) -> None:
    operator = make_bridge(tmp_path, "human-operator", session="operator-before")
    operator.create_room({"room_id": "alpha", "name": "Alpha Room"})
    operator.create_room({"room_id": "beta", "name": "Beta Room"})
    operator.upsert_route_profile(
        {
            "route_id": "kimi-high",
            "agent_id": "kimi-relay",
            "provider_id": "relay-kimi",
            "model_id": "kimi-k2.5",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    operator.join_room(
        {
            "room_id": "alpha",
            "agent_id": "kimi-relay",
            "route_profile_id": "kimi-high",
        }
    )

    restarted = make_bridge(tmp_path, "human-operator", session="operator-after")
    beta_members = restarted.room_members({"room_id": "beta"})["members"]
    assert [row["agent_id"] for row in beta_members] == ["human-operator"]

    alpha_members = restarted.room_members({"room_id": "alpha"})["members"]
    persisted = next(row for row in alpha_members if row["agent_id"] == "kimi-relay")
    assert persisted["route_profile_id"] == "kimi-high"
    assert persisted["provider_id"] == "relay-kimi"
    assert persisted["model_id"] == "kimi-k2.5"
    assert persisted["reasoning_mode"] == "high"


def test_global_agent_catalog_stays_reusable_when_offline_and_seated_in_many_rooms() -> None:
    merged = merge_global_agent_catalog(
        (),
        (
            {
                "agent_id": "grok",
                "online": False,
                "online_sessions": [],
                "route_profiles": [
                    {"route_id": "grok-relay", "provider_id": "relay-grok"}
                ],
                "active_room_ids": ["alpha", "beta"],
                "catalog_sha256": "a" * 64,
            },
        ),
    )
    assert len(merged) == 1
    assert merged[0]["agent_id"] == "grok"
    assert merged[0]["online"] is False
    assert merged[0]["route_ids"] == ("grok-relay",)
    assert merged[0]["active_room_ids"] == ("alpha", "beta")


def test_room_agent_cards_show_global_agents_in_lobby_and_only_members_in_rooms() -> None:
    catalog = (
        {
            "agent_id": "codex-main",
            "provider_id": "openai-official",
            "model_id": "gpt-5.6-sol",
            "online": True,
        },
        {
            "agent_id": "claude-code",
            "provider_id": "anthropic-official",
            "model_id": "claude-opus",
            "online": False,
        },
    )
    lobby = room_agent_cards(DEFAULT_ROOM_ID, (), catalog)
    assert [row["agent_id"] for row in lobby] == [
        "human-operator",
        "claude-code",
        "codex-main",
    ]
    assert all(
        row["state"] == "UNROUTED"
        and row["provider_id"] is None
        and row["model_id"] is None
        for row in lobby
        if row["agent_id"] != "human-operator"
    )

    lobby_without_claude = room_agent_cards(
        DEFAULT_ROOM_ID,
        ({"agent_id": "claude-code", "status": "left"},),
        catalog,
    )
    assert [row["agent_id"] for row in lobby_without_claude] == [
        "human-operator",
        "codex-main",
    ]
    assert active_room_recipient_ids(
        DEFAULT_ROOM_ID,
        ({"agent_id": "claude-code", "status": "left"},),
        ("claude-code", "codex-main"),
    ) == ("codex-main",)

    custom = room_agent_cards(
        "research",
        (
            {
                "agent_id": "human-operator",
                "status": "active",
                "online": False,
            },
            {
                "agent_id": "claude-code",
                "status": "active",
                "online": False,
                "provider_id": None,
                "model_id": None,
            },
        ),
        catalog,
    )
    assert [row["agent_id"] for row in custom] == ["human-operator", "claude-code"]
    assert custom[1]["provider_id"] is None
    assert custom[1]["model_id"] is None
    assert custom[1]["state"] == "UNROUTED"


def test_room_agent_cards_report_the_bound_route_mcp_capability() -> None:
    catalog = (
        {
            "agent_id": "codex-main",
            "provider_id": "openai-official",
            "model_id": "gpt-native",
            "online": True,
            "mcp_access_mode": "MCP_NATIVE",
        },
        {
            "agent_id": "grok-relay",
            "provider_id": "relay-grok",
            "model_id": "grok-4.6",
            "online": True,
            "mcp_access_mode": "MCP_UNVERIFIED",
        },
    )
    cards = room_agent_cards(
        DEFAULT_ROOM_ID,
        (
            {
                "agent_id": "codex-main",
                "status": "active",
                "route_profile_id": "ccswitch-codex",
                "client_name": "codex",
                "provider_id": "ccswitch-codex-example",
                "model_id": "gpt-fallback",
            },
            {
                "agent_id": "grok-relay",
                "status": "active",
                "route_profile_id": "relay-grok",
                "client_name": "openai-compatible",
                "provider_id": "relay-grok",
                "model_id": "grok-4.6",
            },
        ),
        catalog,
    )
    by_agent = {row["agent_id"]: row for row in cards}
    assert by_agent["codex-main"]["mcp_access_mode"] == INFERENCE_ONLY
    assert by_agent["grok-relay"]["mcp_access_mode"] == MCP_TOOL_LOOP


def test_lobby_membership_override_stops_and_restores_delivery(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    grok = make_bridge(
        tmp_path,
        "grok-relay",
        provider_id="relay-grok",
        model_id="grok-4.6",
    )

    removed = human.leave_room(
        {"room_id": DEFAULT_ROOM_ID, "agent_id": "grok-relay"}
    )
    assert removed["status"] == "left"
    with pytest.raises(BridgeError, match="not an active member"):
        grok.poll_messages({"room_id": DEFAULT_ROOM_ID})

    sent = human.send_message(
        {
            "room_id": DEFAULT_ROOM_ID,
            "recipient": "*",
            "task_id": "lobby-override",
            "subject": "hidden",
            "body": "preserve this history",
        }
    )
    assert sent["room_id"] == DEFAULT_ROOM_ID

    restored = human.join_room(
        {"room_id": DEFAULT_ROOM_ID, "agent_id": "grok-relay"}
    )
    assert restored["status"] == "active"
    messages = grok.poll_messages({"room_id": DEFAULT_ROOM_ID})["messages"]
    assert [row["message_id"] for row in messages] == [sent["message_id"]]


def test_lobby_defaults_to_once_and_fans_out_only_to_routed_seats(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    workers: list[Bridge] = []
    for agent_id in ("alpha", "beta"):
        route_id = f"{agent_id}-route"
        provider_id = f"relay-{agent_id}"
        model_id = f"model-{agent_id}"
        human.upsert_route_profile(
            {
                "route_id": route_id,
                "agent_id": agent_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "route_class": "relay",
            }
        )
        human.join_room(
            {
                "room_id": DEFAULT_ROOM_ID,
                "agent_id": agent_id,
                "route_profile_id": route_id,
            }
        )
        workers.append(
            make_bridge(
                tmp_path,
                agent_id,
                provider_id=provider_id,
                model_id=model_id,
                route_class="relay",
            )
        )

    policy = human.get_room_automation({"room_id": DEFAULT_ROOM_ID})
    assert policy["mode"] == "once"
    posted = human.post_room_message(
        {
            "room_id": DEFAULT_ROOM_ID,
            "task_id": "lobby-root-post",
            "subject": "Wake every routed seat once",
            "body": "Each configured Agent should answer once in parallel.",
        }
    )
    assert posted["automation_mode"] == "once"
    assert posted["fanout_count"] == 2

    for worker in workers:
        claim = worker.claim_message_dispatch(
            {
                "room_id": DEFAULT_ROOM_ID,
                "route_profile_id": f"{worker.agent_id}-route",
            }
        )
        assert claim["claimed"] is True
        worker.complete_message_dispatch(
            {
                "message_id": claim["message"]["message_id"],
                "lease_token": claim["lease_token"],
                "body": f"{worker.agent_id} replied once",
                "inference_receipt_sha256": stable_sha256(
                    {"agent_id": worker.agent_id, "task_id": "lobby-root-post"}
                ),
            }
        )
        assert worker.claim_message_dispatch({"room_id": DEFAULT_ROOM_ID})[
            "claimed"
        ] is False

    with sqlite3.connect(human.db_path) as connection:
        rows = connection.execute(
            """SELECT sender, recipient, reply_to
                 FROM messages WHERE task_id='lobby-root-post'
                 ORDER BY sequence"""
        ).fetchall()
    assert len(rows) == 4
    assert sum(row[2] is None for row in rows) == 2
    assert sum(row[2] is not None for row in rows) == 2


def test_user_selected_lobby_off_survives_schema_reopen(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.set_room_automation({"room_id": DEFAULT_ROOM_ID, "mode": "off"})

    reopened = make_bridge(tmp_path, "another-agent")
    policy = reopened.get_room_automation({"room_id": DEFAULT_ROOM_ID})
    assert policy["mode"] == "off"
    assert policy["updated_by"] == "human-operator"


def test_room_seat_route_resolution_is_exact_and_fanout_rejects_unrouted_seats() -> None:
    profiles = (
        {
            "route_id": "kimi-high",
            "provider_id": "relay-kimi",
            "model_id": "kimi-k2.5",
            "reasoning_mode": "high",
        },
        {
            "route_id": "kimi-low",
            "provider_id": "relay-kimi",
            "model_id": "kimi-k2.5",
            "reasoning_mode": "low",
        },
    )
    selected = exact_route_profile(
        profiles,
        provider_id="relay-kimi",
        model_id="kimi-k2.5",
        reasoning_mode="high",
    )
    assert selected and selected["route_id"] == "kimi-high"
    assert exact_route_profile(
        (*profiles, dict(profiles[0], route_id="kimi-high-duplicate")),
        provider_id="relay-kimi",
        model_id="kimi-k2.5",
        reasoning_mode="high",
    ) is None
    assert room_members_missing_routes(
        (
            {"agent_id": "human-operator", "status": "active", "route_profile_id": None},
            {"agent_id": "kimi-relay", "status": "active", "route_profile_id": None},
            {"agent_id": "grok-relay", "status": "active", "route_profile_id": "grok"},
            {"agent_id": "old-agent", "status": "left", "route_profile_id": None},
        )
    ) == ("kimi-relay",)


def test_agent_route_options_merge_registered_and_live_advertised_models() -> None:
    profiles = (
        {
            "route_id": "grok-registered",
            "agent_id": "grok-relay",
            "client_name": "openai-compatible",
            "provider_id": "relay-grok",
            "model_id": "grok-4.6",
            "route_class": "relay",
            "enabled": True,
        },
        {
            "route_id": "other-agent",
            "agent_id": "kimi-relay",
            "provider_id": "relay-grok",
            "model_id": "k3",
            "route_class": "relay",
            "enabled": True,
        },
    )
    connections = (
        {
            "connection_id": "relay-grok",
            "route_class": "relay",
            "enabled": True,
        },
        {
            "connection_id": "disabled-provider",
            "route_class": "relay",
            "enabled": False,
        },
    )
    options = agent_route_options(
        "grok-relay",
        profiles,
        connections,
        {
            "relay-grok": ("grok-4.6", "grok-4.20-reasoning", "grok-4.20-reasoning"),
            "disabled-provider": ("must-not-appear",),
        },
        {"relay-grok": "a" * 64},
    )

    assert [row["model_id"] for row in options] == [
        "grok-4.20-reasoning",
        "grok-4.6",
    ]
    advertised = options[0]
    assert advertised["_advertised_only"] is True
    assert advertised["route_id"] is None
    assert advertised["_registry_sha256"] == "a" * 64
    assert options[1].get("_advertised_only") is None


def test_grok_and_kimi_catalogs_remain_separate() -> None:
    profiles = (
        {
            "route_id": "grok-registered",
            "agent_id": "grok-relay",
            "client_name": "openai-compatible",
            "provider_id": "relay-grok",
            "model_id": "grok-4.6",
            "route_class": "relay",
            "enabled": True,
        },
        {
            "route_id": "kimi-registered",
            "agent_id": "kimi-relay",
            "client_name": "openai-compatible",
            "provider_id": "relay-kimi",
            "model_id": "kimi-for-coding",
            "route_class": "relay",
            "enabled": True,
        },
    )
    connections = (
        {"connection_id": "relay-grok", "route_class": "relay", "enabled": True},
        {"connection_id": "relay-kimi", "route_class": "relay", "enabled": True},
    )
    advertised = {
        "relay-grok": ("grok-4.6", "grok-4.20-reasoning"),
        "relay-kimi": ("kimi-for-coding", "kimi-k2.5"),
    }

    grok = agent_route_options(
        "grok-relay", profiles, connections, advertised, {"relay-grok": "a" * 64}
    )
    kimi = agent_route_options(
        "kimi-relay", profiles, connections, advertised, {"relay-kimi": "b" * 64}
    )

    assert {row["model_id"] for row in grok} == {
        "grok-4.6",
        "grok-4.20-reasoning",
    }
    assert {row["provider_id"] for row in grok} == {"relay-grok"}
    assert {row["model_id"] for row in kimi} == {"kimi-for-coding", "kimi-k2.5"}
    assert {row["provider_id"] for row in kimi} == {"relay-kimi"}


def test_codex_catalog_options_include_all_visible_models_and_reasoning() -> None:
    catalog = CodexModelCatalog(
        models=(
            CodexModel("gpt-5.6-sol", "Sol", "high", ("low", "high", "ultra"), 0),
            CodexModel("gpt-5.6-terra", "Terra", "high", ("low", "high", "ultra"), 1),
            CodexModel("gpt-5.6-luna", "Luna", "high", ("low", "high"), 2),
            CodexModel("gpt-5.5", "GPT-5.5", "high", ("low", "high"), 3),
            CodexModel("gpt-5.4", "GPT-5.4", "high", ("low", "high"), 4),
            CodexModel("gpt-5.4-mini", "GPT-5.4 mini", "high", ("low", "high"), 5),
            CodexModel(
                "gpt-5.3-codex-spark", "GPT-5.3 Codex Spark", "high", ("low", "high"), 6
            ),
        ),
        catalog_sha256="c" * 64,
    )

    options = codex_catalog_route_options("codex-main", catalog)

    assert {row["model_id"] for row in options} == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
    }
    assert {
        row["reasoning_mode"]
        for row in options
        if row["model_id"] == "gpt-5.6-sol"
    } == {"low", "high", "ultra"}
    assert all(row["provider_id"] == "openai-official" for row in options)
    assert all(row["_catalog_source"] == "codex-cli" for row in options)
    assert codex_catalog_route_options("grok-relay", catalog) == ()


def test_registered_codex_route_overrides_same_discovered_choice() -> None:
    registered = ({
        "route_id": "codex-sol-ultra",
        "agent_id": "codex-main",
        "provider_id": "openai-official",
        "model_id": "gpt-5.6-sol",
        "reasoning_mode": "ultra",
        "route_class": "official",
        "enabled": True,
    },)
    discovered = ({
        "route_id": None,
        "agent_id": "codex-main",
        "provider_id": "openai-official",
        "model_id": "gpt-5.6-sol",
        "reasoning_mode": "ultra",
        "route_class": "official",
        "enabled": True,
        "_advertised_only": True,
    },)

    merged = merge_agent_route_options(discovered, registered)

    assert merged == registered


def test_point_in_rectangle_accepts_whole_sidebar_and_rejects_outside() -> None:
    kwargs = {"left": 10, "top": 20, "width": 236, "height": 780}
    assert point_in_rectangle(11, 799, **kwargs)
    assert point_in_rectangle(246, 800, **kwargs)
    assert not point_in_rectangle(247, 800, **kwargs)
    assert not point_in_rectangle(200, 801, **kwargs)
    assert not point_in_rectangle(10, 20, left=10, top=20, width=0, height=780)


def test_discovered_route_profile_id_is_stable_and_binds_full_selection() -> None:
    first = discovered_route_profile_id(
        scope="peerbridge-main",
        agent_id="grok-relay",
        provider_id="relay-grok",
        model_id="grok-4.20-reasoning",
        reasoning_mode=None,
    )
    replay = discovered_route_profile_id(
        scope="peerbridge-main",
        agent_id="grok-relay",
        provider_id="relay-grok",
        model_id="grok-4.20-reasoning",
        reasoning_mode=None,
    )
    changed = discovered_route_profile_id(
        scope="peerbridge-main",
        agent_id="grok-relay",
        provider_id="relay-grok",
        model_id="grok-4.20-non-reasoning",
        reasoning_mode=None,
    )

    assert first == replay
    assert first != changed
    assert first.startswith("discovered-")
    assert len(first) == len("discovered-") + 40


def test_provider_labels_use_readable_connection_name_and_specs_are_redacted() -> None:
    provider = CcSwitchProvider(
        app="codex",
        provider_id="relay-provider-1234567890",
        name="Relay One",
        current=False,
        has_endpoint=True,
    )
    connection, routes = ccswitch_route_specs(
        provider,
        agent_id="codex-main",
        models=("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-sol"),
        reasoning_mode="ultra",
    )
    assert connection["secret_backend"] == "cc-switch"
    assert "https://" not in json.dumps(connection)
    assert len(routes) == 2
    assert {row["model_id"] for row in routes} == {"gpt-5.6-sol", "gpt-5.6-luna"}
    assert all(row["agent_id"] == "codex-main" for row in routes)
    assert all(row["reasoning_mode"] == "ultra" for row in routes)
    assert provider_display_label(routes[0], (connection,)).startswith(
        "RELAY | CC Switch / Relay One"
    )


def test_room_reply_and_broadcast_fail_closed_across_boundaries(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "one", "name": "One"})
    human.create_room({"room_id": "two", "name": "Two"})
    human.join_room({"room_id": "one", "agent_id": "peer"})
    first = human.send_message(
        {
            "room_id": "one",
            "recipient": "*",
            "task_id": "one-task",
            "subject": "room broadcast",
            "body": "Only active room members may consume this.",
        }
    )
    peer = make_bridge(tmp_path, "peer")
    assert peer.poll_messages({"room_id": "one"})["count"] == 1
    with pytest.raises(BridgeError, match="not an active member"):
        peer.poll_messages({"room_id": "two"})
    with pytest.raises(BridgeError, match="same room"):
        human.send_message(
            {
                "room_id": "two",
                "recipient": "*",
                "task_id": "two-task",
                "subject": "bad reply",
                "body": "Must not cross contexts.",
                "reply_to": first["message_id"],
            }
        )


def test_room_fanout_is_atomic_routed_and_room_scoped(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "team", "name": "Team"})
    profile_sha_by_route: dict[str, str] = {}
    for agent, route, provider, model in (
        ("grok", "grok-route", "relay-grok", "grok-4.6"),
        ("kimi", "kimi-route", "relay-kimi", "kimi-for-coding"),
    ):
        profile = human.upsert_route_profile(
            {
                "route_id": route,
                "agent_id": agent,
                "provider_id": provider,
                "model_id": model,
                "route_class": "relay",
            }
        )
        profile_sha_by_route[route] = profile["profile_sha256"]
        human.join_room(
            {"room_id": "team", "agent_id": agent, "route_profile_id": route}
        )

    receipt = human.send_room_fanout(
        {
            "room_id": "team",
            "task_id": "team-question",
            "subject": "Answer once",
            "body": "One room input for every routed seat.",
        }
    )

    assert receipt["fanout_count"] == 2
    assert [row["agent_id"] for row in receipt["recipients"]] == ["grok", "kimi"]
    with sqlite3.connect(human.db_path) as connection:
        rows = connection.execute(
            "SELECT room_id, recipient, route_profile_id, route_profile_sha256, "
            "route_request_sha256 "
            "FROM messages ORDER BY sequence"
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("team", "grok", "grok-route"),
        ("team", "kimi", "kimi-route"),
    ]
    assert all(row[3] == profile_sha_by_route[row[2]] for row in rows)
    assert all(row[4] for row in rows)
    assert all(
        row["route_profile_sha256"]
        == profile_sha_by_route[row["route_profile_id"]]
        for row in receipt["recipients"]
    )

    grok = make_bridge(
        tmp_path,
        "grok",
        provider_id="relay-grok",
        model_id="grok-4.6",
        route_class="relay",
    )
    kimi = make_bridge(
        tmp_path,
        "kimi",
        provider_id="relay-kimi",
        model_id="kimi-for-coding",
        route_class="relay",
    )
    grok_delivery = next(
        row for row in receipt["recipients"] if row["agent_id"] == "grok"
    )
    with pytest.raises(BridgeError, match="route_profile_id is required"):
        grok.claim_message_dispatch({"message_id": grok_delivery["message_id"]})
    grok_claim = grok.claim_message_dispatch({})
    assert grok_claim["claimed"] is True
    assert (
        grok_claim["message"]["route_request"]["route_profile_sha256"]
        == profile_sha_by_route["grok-route"]
    )
    kimi_claim = kimi.claim_message_dispatch(
        {"room_id": "team", "route_profile_id": "kimi-route"}
    )
    assert kimi_claim["claimed"] is True
    assert (
        kimi_claim["message"]["route_request"]["route_profile_sha256"]
        == profile_sha_by_route["kimi-route"]
    )
    assert grok.claim_message_dispatch({"room_id": "team"})["claimed"] is False
    assert kimi.claim_message_dispatch({"room_id": "team"})["claimed"] is False


def test_room_fanout_with_unrouted_seat_writes_nothing(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "strict", "name": "Strict"})
    human.upsert_route_profile(
        {
            "route_id": "grok-route",
            "agent_id": "grok",
            "provider_id": "relay-grok",
            "model_id": "grok-4.6",
            "route_class": "relay",
        }
    )
    human.join_room(
        {"room_id": "strict", "agent_id": "grok", "route_profile_id": "grok-route"}
    )
    human.join_room({"room_id": "strict", "agent_id": "unrouted"})

    with pytest.raises(BridgeError, match="has no route profile"):
        human.send_room_fanout(
            {
                "room_id": "strict",
                "task_id": "must-be-atomic",
                "subject": "No partial send",
                "body": "Every seat or none.",
            }
        )
    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='message.room_fanout_sent'"
        ).fetchone()[0] == 0


def _automated_room(
    tmp_path: Path,
    *,
    room_id: str = "automation",
) -> tuple[Bridge, Bridge, Bridge]:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": room_id, "name": "Automation Room"})
    workers: list[Bridge] = []
    for agent_id in ("alpha", "beta"):
        route_id = f"{agent_id}-route"
        provider_id = f"relay-{agent_id}"
        model_id = f"model-{agent_id}"
        human.upsert_route_profile(
            {
                "route_id": route_id,
                "agent_id": agent_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "reasoning_mode": "high",
                "route_class": "relay",
            }
        )
        human.join_room(
            {
                "room_id": room_id,
                "agent_id": agent_id,
                "route_profile_id": route_id,
            }
        )
        workers.append(
            make_bridge(
                tmp_path,
                agent_id,
                provider_id=provider_id,
                model_id=model_id,
                reasoning_mode="high",
                route_class="relay",
            )
        )
    return human, workers[0], workers[1]


def _complete_discussion_prompt(worker: Bridge, room_id: str, signal: str) -> str:
    with sqlite3.connect(worker.db_path) as connection:
        route_id = connection.execute(
            """SELECT route_profile_id FROM room_memberships
                 WHERE scope=? AND room_id=? AND agent_id=? AND status='active'""",
            (worker.scope, room_id, worker.agent_id),
        ).fetchone()[0]
    claim = worker.claim_message_dispatch(
        {
            "room_id": room_id,
            "require_route": True,
            "route_profile_id": route_id,
        }
    )
    assert claim["claimed"] is True
    message_id = str(claim["message"]["message_id"])
    worker.complete_message_dispatch(
        {
            "message_id": message_id,
            "lease_token": claim["lease_token"],
            "body": f"Independent contribution from {worker.agent_id}.\n\n"
            f"PEERBRIDGE_SIGNAL: {signal}",
            "inference_receipt_sha256": stable_sha256(
                {"message_id": message_id, "signal": signal}
            ),
        }
    )
    return message_id


def _force_discussion_boundary(
    bridge: Bridge, discussion_id: str, **changes: object
) -> None:
    with sqlite3.connect(bridge.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM room_discussions WHERE discussion_id=?",
            (discussion_id,),
        ).fetchone()
        assert row is not None
        payload = {**Bridge._discussion_row_payload(row), **changes}
        connection.execute(
            """UPDATE room_discussions
                  SET status=?, current_round=?, processed_round=?, max_rounds=?,
                      max_messages=?, message_count=?, stop_reason=?,
                      discussion_sha256=?
                WHERE discussion_id=?""",
            (
                payload["status"],
                payload["current_round"],
                payload["processed_round"],
                payload["max_rounds"],
                payload["max_messages"],
                payload["message_count"],
                payload["stop_reason"],
                stable_sha256(payload),
                discussion_id,
            ),
        )


def test_room_automation_off_records_history_without_dispatch(tmp_path: Path) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="quiet-room")
    policy = human.set_room_automation(
        {"room_id": "quiet-room", "mode": "off"}
    )
    posted = human.post_room_message(
        {
            "room_id": "quiet-room",
            "task_id": "quiet-post",
            "subject": "History only",
            "body": "Record this without waking any Agent.",
        }
    )

    assert policy["mode"] == "off"
    assert posted["automation_mode"] == "off"
    assert posted["fanout_count"] == 0
    assert alpha.claim_message_dispatch({"room_id": "quiet-room"})["claimed"] is False
    assert beta.claim_message_dispatch({"room_id": "quiet-room"})["claimed"] is False
    assert human.poll_messages({"room_id": "quiet-room", "include_sent": True})["count"] == 1


def test_room_automation_once_fans_out_exactly_one_parallel_round(
    tmp_path: Path,
) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="once-room")
    posted = human.post_room_message(
        {
            "room_id": "once-room",
            "task_id": "once-post",
            "subject": "One response each",
            "body": "Reply once in parallel.",
        }
    )
    assert posted["automation_mode"] == "once"
    assert posted["fanout_count"] == 2

    _complete_discussion_prompt(alpha, "once-room", "CONTINUE")
    _complete_discussion_prompt(beta, "once-room", "CONTINUE")
    assert alpha.claim_message_dispatch({"room_id": "once-room"})["claimed"] is False
    assert beta.claim_message_dispatch({"room_id": "once-room"})["claimed"] is False
    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM room_discussions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE task_id='once-post'"
        ).fetchone()[0] == 4


@pytest.mark.parametrize("mode", ["off", "discussion"])
def test_direct_room_fanout_cannot_bypass_room_policy(
    tmp_path: Path, mode: str
) -> None:
    human, _alpha, _beta = _automated_room(tmp_path, room_id="policy-room")
    human.set_room_automation(
        {
            "room_id": "policy-room",
            "mode": mode,
            "max_rounds": 2,
            "max_messages": 12,
            "stagnation_rounds": 1,
        }
    )
    with sqlite3.connect(human.db_path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    with pytest.raises(BridgeError, match="requires automation mode once"):
        human.send_room_fanout(
            {
                "room_id": "policy-room",
                "task_id": "policy-bypass",
                "subject": "Do not bypass policy",
                "body": "This direct fanout must remain atomic and unwritten.",
            }
        )

    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
        assert connection.execute(
            "SELECT COUNT(*) FROM events WHERE task_id='policy-bypass'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("mode", "expected_messages"),
    [("off", 1), ("once", 2), ("discussion", 2)],
)
def test_room_posts_bind_safe_attachments_in_every_initial_delivery(
    tmp_path: Path, mode: str, expected_messages: int
) -> None:
    human, _alpha, _beta = _automated_room(tmp_path, room_id="attachment-room")
    human.set_room_automation(
        {
            "room_id": "attachment-room",
            "mode": mode,
            "max_rounds": 3,
            "max_messages": 12,
            "stagnation_rounds": 2,
        }
    )
    source = tmp_path / "screen.png"
    source.write_bytes(PNG)
    staged = stage_chat_attachments(tmp_path, [source])
    artifact = staged[0].relative_path

    human.post_room_message(
        {
            "room_id": "attachment-room",
            "task_id": "attachment-post",
            "subject": "Bound evidence",
            "body": "Use the attached evidence only when the provider supports it.",
            "artifact_paths": [artifact],
        }
    )

    with sqlite3.connect(human.db_path) as connection:
        rows = connection.execute(
            "SELECT artifact_paths_json FROM messages WHERE task_id='attachment-post'"
        ).fetchall()
    assert len(rows) == expected_messages
    assert all(json.loads(row[0]) == [artifact] for row in rows)


def test_discussion_continuation_does_not_repeat_initial_attachments(
    tmp_path: Path,
) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="attachment-discussion")
    human.set_room_automation(
        {
            "room_id": "attachment-discussion",
            "mode": "discussion",
            "max_rounds": 3,
            "max_messages": 12,
            "stagnation_rounds": 2,
        }
    )
    source = tmp_path / "chart.png"
    source.write_bytes(PNG)
    artifact = stage_chat_attachments(tmp_path, [source])[0].relative_path
    human.post_room_message(
        {
            "room_id": "attachment-discussion",
            "task_id": "attachment-discussion-post",
            "subject": "Discuss evidence",
            "body": "Review this once, then discuss conclusions.",
            "artifact_paths": [artifact],
        }
    )
    _complete_discussion_prompt(alpha, "attachment-discussion", "CONTINUE")
    _complete_discussion_prompt(beta, "attachment-discussion", "CONTINUE")
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    assert coordinator.advance_discussions({"room_id": "attachment-discussion"})[
        "count"
    ] == 1

    with sqlite3.connect(human.db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT m.*, rp.profile_sha256 AS current_profile_sha256
                 FROM messages m
                 LEFT JOIN route_profiles rp
                   ON rp.scope=m.scope AND rp.route_id=m.route_profile_id
                WHERE m.task_id='attachment-discussion-post'
                ORDER BY m.sequence"""
        ).fetchall()
    initial = [
        json.loads(row["artifact_paths_json"])
        for row in rows
        if row["discussion_round"] == 1 and row["discussion_role"] == "prompt"
    ]
    continued = [
        json.loads(row["artifact_paths_json"])
        for row in rows
        if row["discussion_round"] == 2 and row["discussion_role"] == "prompt"
    ]
    assert initial == [[artifact], [artifact]]
    assert continued == [[], []]
    prompts = [row for row in rows if row["discussion_role"] == "prompt"]
    assert len(prompts) == 4
    assert all(
        row["route_profile_sha256"] == row["current_profile_sha256"]
        for row in prompts
    )
    for row in prompts:
        route_request = Bridge._route_request_from_row(row)
        assert route_request is not None
        assert (
            route_request["route_profile_sha256"]
            == row["current_profile_sha256"]
        )
        assert route_request["route_request_sha256"] == stable_sha256(
            {
                key: value
                for key, value in route_request.items()
                if key != "route_request_sha256"
            }
        )


def test_staged_chat_attachment_must_remain_content_addressed(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    source = tmp_path / "chart.png"
    source.write_bytes(PNG)
    artifact = stage_chat_attachments(tmp_path, [source])[0].relative_path
    (tmp_path / artifact).write_bytes(PNG[:-1] + bytes([PNG[-1] ^ 1]))

    with pytest.raises(BridgeError, match="content-addressed"):
        human.send_message(
            {
                "recipient": "reviewer",
                "task_id": "tampered-attachment",
                "subject": "Reject drift",
                "body": "This message must not be written.",
                "artifact_paths": [artifact],
            }
        )


def test_agent_task_scope_cannot_write_chat_attachment_store(tmp_path: Path) -> None:
    owner = make_bridge(tmp_path, "owner")
    with pytest.raises(BridgeError, match="protected or sensitive"):
        owner.claim_task(
            {
                "task_id": "protected-attachment-store",
                "summary": "A task cannot claim the private attachment staging area.",
                "write_paths": [".peerbridge-artifacts/chat"],
            }
        )


def test_discussion_fails_closed_when_budget_cannot_fit_all_seats(
    tmp_path: Path,
) -> None:
    human, _alpha, _beta = _automated_room(tmp_path, room_id="budget-room")
    human.set_room_automation(
        {
            "room_id": "budget-room",
            "mode": "discussion",
            "max_rounds": 1,
            "max_messages": 2,
            "stagnation_rounds": 1,
        }
    )

    with pytest.raises(BridgeError, match="requires at least 4"):
        human.post_room_message(
            {
                "room_id": "budget-room",
                "task_id": "insufficient-budget",
                "subject": "Must not partially wake seats",
                "body": "A complete round needs one prompt and response per seat.",
            }
        )

    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM room_discussions WHERE room_id='budget-room'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE task_id='insufficient-budget'"
        ).fetchone()[0] == 0


def test_bounded_discussion_advances_once_then_stops_on_consensus(
    tmp_path: Path,
) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="discussion-room")
    human.set_room_automation(
        {
            "room_id": "discussion-room",
            "mode": "discussion",
            "max_rounds": 4,
            "max_messages": 20,
            "stagnation_rounds": 2,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "discussion-room",
            "task_id": "bounded-discussion",
            "subject": "Reach consensus",
            "body": "Discuss in parallel and stop at consensus.",
        }
    )
    discussion_id = str(posted["discussion_id"])
    assert posted["fanout_count"] == 2

    _complete_discussion_prompt(alpha, "discussion-room", "CONTINUE")
    _complete_discussion_prompt(beta, "discussion-room", "CONTINUE")
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    first = coordinator.advance_discussions({"room_id": "discussion-room"})
    duplicate = coordinator.advance_discussions({"room_id": "discussion-room"})
    assert first["count"] == 1
    assert first["advanced"][0]["current_round"] == 2
    assert first["advanced"][0]["new_prompt_count"] == 2
    assert duplicate["count"] == 0

    _complete_discussion_prompt(alpha, "discussion-room", "CONSENSUS")
    _complete_discussion_prompt(beta, "discussion-room", "CONSENSUS")
    final = coordinator.advance_discussions({"room_id": "discussion-room"})
    assert final["advanced"][0]["status"] == "completed"
    assert final["advanced"][0]["stop_reason"] == "consensus"
    assert final["advanced"][0]["new_prompt_count"] == 0
    assert coordinator.advance_discussions({"room_id": "discussion-room"})["count"] == 0

    state = human.get_room_automation({"room_id": "discussion-room"})
    assert state["active_discussion"] is None
    with sqlite3.connect(human.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM room_discussions WHERE discussion_id=?",
            (discussion_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "completed"
        assert row["message_count"] == 8
        assert row["discussion_sha256"] == stable_sha256(
            Bridge._discussion_row_payload(row)
        )


@pytest.mark.parametrize(
    ("signals", "max_rounds", "max_messages", "expected_reason"),
    [
        (("BLOCKED", "BLOCKED"), 4, 20, "all_agents_blocked"),
        (("CONTINUE", "CONTINUE"), 1, 20, "round_limit"),
        (("CONTINUE", "CONTINUE"), 4, 6, "message_limit"),
    ],
)
def test_discussion_terminal_limits_are_enforced(
    tmp_path: Path,
    signals: tuple[str, str],
    max_rounds: int,
    max_messages: int,
    expected_reason: str,
) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="limited-room")
    human.set_room_automation(
        {
            "room_id": "limited-room",
            "mode": "discussion",
            "max_rounds": max_rounds,
            "max_messages": max_messages,
            "stagnation_rounds": min(2, max_rounds),
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "limited-room",
            "task_id": f"limit-{expected_reason}",
            "subject": "Bounded termination",
            "body": "Stop on the configured terminal condition.",
        }
    )
    _complete_discussion_prompt(alpha, "limited-room", signals[0])
    _complete_discussion_prompt(beta, "limited-room", signals[1])

    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    advanced = coordinator.advance_discussions({"room_id": "limited-room"})

    assert advanced["count"] == 1
    assert advanced["advanced"][0]["status"] == "waiting_human"
    assert advanced["advanced"][0]["stop_reason"] == expected_reason
    assert advanced["advanced"][0]["new_prompt_count"] == 0
    with sqlite3.connect(human.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()
        assert row is not None
        assert row["discussion_sha256"] == stable_sha256(
            Bridge._discussion_row_payload(row)
        )


@pytest.mark.parametrize(
    ("boundary", "expected_error"),
    [
        (
            {
                "current_round": 20,
                "processed_round": 20,
                "max_rounds": 20,
                "message_count": 4,
            },
            "absolute round limit",
        ),
        (
            {
                "current_round": 1,
                "processed_round": 1,
                "max_rounds": 20,
                "max_messages": 200,
                "message_count": 197,
            },
            "absolute message limit",
        ),
    ],
)
def test_human_continue_cannot_expand_past_absolute_discussion_limits(
    tmp_path: Path, boundary: dict[str, int], expected_error: str
) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="absolute-limit-room")
    human.set_room_automation(
        {
            "room_id": "absolute-limit-room",
            "mode": "discussion",
            "max_rounds": 1,
            "max_messages": 20,
            "stagnation_rounds": 1,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "absolute-limit-room",
            "task_id": "absolute-limit",
            "subject": "Absolute discussion limits",
            "body": "Reach a terminal state before testing human continuation.",
        }
    )
    _complete_discussion_prompt(alpha, "absolute-limit-room", "CONTINUE")
    _complete_discussion_prompt(beta, "absolute-limit-room", "CONTINUE")
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    terminal = coordinator.advance_discussions({"room_id": "absolute-limit-room"})
    assert terminal["advanced"][0]["status"] == "waiting_human"

    _force_discussion_boundary(
        human,
        str(posted["discussion_id"]),
        status="waiting_human",
        stop_reason="hard_limit_fixture",
        **boundary,
    )
    with sqlite3.connect(human.db_path) as connection:
        messages_before = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()[0]

    with pytest.raises(BridgeError, match=expected_error):
        human.control_discussion(
            {
                "discussion_id": posted["discussion_id"],
                "action": "continue",
                "extra_rounds": 10,
            }
        )

    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()[0] == messages_before


def test_discussion_stagnation_continue_and_stop_controls(tmp_path: Path) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="stagnant-room")
    human.set_room_automation(
        {
            "room_id": "stagnant-room",
            "mode": "discussion",
            "max_rounds": 4,
            "max_messages": 20,
            "stagnation_rounds": 1,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "stagnant-room",
            "task_id": "stagnation-control",
            "subject": "Detect repetition",
            "body": "Repeated identical contributions must stop automatically.",
        }
    )
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    for worker in (alpha, beta):
        _complete_discussion_prompt(worker, "stagnant-room", "CONTINUE")
    first = coordinator.advance_discussions({"room_id": "stagnant-room"})
    assert first["advanced"][0]["current_round"] == 2

    for worker in (alpha, beta):
        _complete_discussion_prompt(worker, "stagnant-room", "CONTINUE")
    stagnant = coordinator.advance_discussions({"room_id": "stagnant-room"})
    assert stagnant["advanced"][0]["status"] == "waiting_human"
    assert stagnant["advanced"][0]["stop_reason"] == "stagnation"

    continued = human.control_discussion(
        {
            "discussion_id": posted["discussion_id"],
            "action": "continue",
            "extra_rounds": 1,
        }
    )
    assert continued["status"] == "active"
    assert continued["current_round"] == 3
    stopped = human.control_discussion(
        {"discussion_id": posted["discussion_id"], "action": "stop"}
    )
    assert stopped["status"] == "stopped"
    assert stopped["stop_reason"] == "human_stopped"
    assert alpha.claim_message_dispatch({"room_id": "stagnant-room"})["claimed"] is False


def test_paused_discussion_blocks_claims_until_human_resumes(tmp_path: Path) -> None:
    human, alpha, _beta = _automated_room(tmp_path, room_id="paused-room")
    human.set_room_automation(
        {
            "room_id": "paused-room",
            "mode": "discussion",
            "max_rounds": 2,
            "max_messages": 12,
            "stagnation_rounds": 1,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "paused-room",
            "task_id": "pause-control",
            "subject": "Human control",
            "body": "Wait when a human pauses the discussion.",
        }
    )
    discussion_id = str(posted["discussion_id"])
    paused = human.control_discussion(
        {"discussion_id": discussion_id, "action": "pause"}
    )
    assert paused["status"] == "paused"
    assert alpha.claim_message_dispatch({"room_id": "paused-room"})["claimed"] is False

    resumed = human.control_discussion(
        {"discussion_id": discussion_id, "action": "resume"}
    )
    assert resumed["status"] == "active"
    assert alpha.claim_message_dispatch(
        {"room_id": "paused-room", "route_profile_id": "alpha-route"}
    )["claimed"] is True


def test_paused_discussion_accepts_only_already_claimed_reply(tmp_path: Path) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="paused-inflight-room")
    human.set_room_automation(
        {
            "room_id": "paused-inflight-room",
            "mode": "discussion",
            "max_rounds": 2,
            "max_messages": 12,
            "stagnation_rounds": 1,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "paused-inflight-room",
            "task_id": "pause-inflight",
            "subject": "Preserve in-flight work",
            "body": "An already claimed reply may finish while new work stays paused.",
        }
    )
    claim = alpha.claim_message_dispatch(
        {"room_id": "paused-inflight-room", "route_profile_id": "alpha-route"}
    )
    assert claim["claimed"] is True

    paused = human.control_discussion(
        {"discussion_id": posted["discussion_id"], "action": "pause"}
    )
    assert paused["status"] == "paused"
    assert beta.claim_message_dispatch({"room_id": "paused-inflight-room"})[
        "claimed"
    ] is False

    message_id = str(claim["message"]["message_id"])
    completed = alpha.complete_message_dispatch(
        {
            "message_id": message_id,
            "lease_token": claim["lease_token"],
            "body": "The pre-pause inference completed safely.\n\n"
            "PEERBRIDGE_SIGNAL: CONTINUE",
            "inference_receipt_sha256": stable_sha256(
                {"message_id": message_id, "state": "completed-while-paused"}
            ),
        }
    )
    assert completed["completed"] is True
    with sqlite3.connect(human.db_path) as connection:
        discussion_status = connection.execute(
            "SELECT status FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()[0]
        dispatch_status = connection.execute(
            "SELECT status FROM message_dispatches WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
    assert discussion_status == "paused"
    assert dispatch_status == "completed"

    resumed = human.control_discussion(
        {"discussion_id": posted["discussion_id"], "action": "resume"}
    )
    assert resumed["status"] == "active"
    assert beta.claim_message_dispatch(
        {"room_id": "paused-inflight-room", "route_profile_id": "beta-route"}
    )["claimed"] is True


def test_stopping_discussion_fences_an_inflight_reply(tmp_path: Path) -> None:
    human, alpha, _beta = _automated_room(tmp_path, room_id="stop-fence-room")
    human.set_room_automation(
        {
            "room_id": "stop-fence-room",
            "mode": "discussion",
            "max_rounds": 2,
            "max_messages": 12,
            "stagnation_rounds": 1,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "stop-fence-room",
            "task_id": "stop-fence",
            "subject": "Fence stale reply",
            "body": "A stopped discussion must reject an in-flight completion.",
        }
    )
    claim = alpha.claim_message_dispatch(
        {"room_id": "stop-fence-room", "route_profile_id": "alpha-route"}
    )
    assert claim["claimed"] is True
    stopped = human.control_discussion(
        {"discussion_id": posted["discussion_id"], "action": "stop"}
    )
    assert stopped["status"] == "stopped"
    with pytest.raises(BridgeError, match="no active lease"):
        alpha.complete_message_dispatch(
            {
                "message_id": claim["message"]["message_id"],
                "lease_token": claim["lease_token"],
                "body": "This stale response must not be committed.",
                "inference_receipt_sha256": "a" * 64,
            }
        )
    with sqlite3.connect(human.db_path) as connection:
        row = connection.execute(
            "SELECT status, error_code FROM message_dispatches WHERE message_id=?",
            (claim["message"]["message_id"],),
        ).fetchone()
    assert row == ("failed", "discussion_stopped")


@pytest.mark.parametrize(
    "body",
    [
        "No signal is present.",
        "PEERBRIDGE_SIGNAL: CONSENSUS\nTrailing text is forbidden.",
        "PEERBRIDGE_SIGNAL: CONTINUE\nPEERBRIDGE_SIGNAL: CONSENSUS",
        "prefix PEERBRIDGE_SIGNAL: CONSENSUS",
    ],
)
def test_discussion_signal_contract_rejects_ambiguous_text(body: str) -> None:
    assert Bridge._discussion_signal(body) == "INVALID"


def test_discussion_participant_route_change_waits_for_human(tmp_path: Path) -> None:
    human, alpha, beta = _automated_room(tmp_path, room_id="route-change-room")
    human.set_room_automation(
        {
            "room_id": "route-change-room",
            "mode": "discussion",
            "max_rounds": 3,
            "max_messages": 20,
            "stagnation_rounds": 2,
        }
    )
    posted = human.post_room_message(
        {
            "room_id": "route-change-room",
            "task_id": "route-change",
            "subject": "Immutable participants",
            "body": "Changing a participant route must stop before another round.",
        }
    )
    _complete_discussion_prompt(alpha, "route-change-room", "CONTINUE")
    _complete_discussion_prompt(beta, "route-change-room", "CONTINUE")
    with sqlite3.connect(human.db_path) as connection:
        message_count_before = connection.execute(
            "SELECT message_count FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()[0]
    human.upsert_route_profile(
        {
            "route_id": "beta-route-v2",
            "agent_id": "beta",
            "provider_id": "relay-beta-v2",
            "model_id": "model-beta-v2",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    human.leave_room({"room_id": "route-change-room", "agent_id": "beta"})
    human.join_room(
        {
            "room_id": "route-change-room",
            "agent_id": "beta",
            "route_profile_id": "beta-route-v2",
        }
    )
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    result = coordinator.advance_discussions({"room_id": "route-change-room"})
    assert result["count"] == 1
    assert result["advanced"][0]["status"] == "waiting_human"
    assert result["advanced"][0]["stop_reason"] == "participant_unavailable"
    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE discussion_id=? AND discussion_round=2",
            (posted["discussion_id"],),
        ).fetchone()[0] == 0
        message_count_after = connection.execute(
            "SELECT message_count FROM room_discussions WHERE discussion_id=?",
            (posted["discussion_id"],),
        ).fetchone()[0]
        event_payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM events WHERE event_type='discussion.round_blocked' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert message_count_after == message_count_before + 2
    assert event_payload["response_count"] == 2


def test_coordinator_reconciles_unavailable_prompt_without_audit_churn(
    tmp_path: Path,
) -> None:
    human, _alpha, _beta = _automated_room(tmp_path, room_id="reconcile-room")
    human.set_room_automation(
        {
            "room_id": "reconcile-room",
            "mode": "discussion",
            "max_rounds": 2,
            "max_messages": 12,
            "stagnation_rounds": 1,
        }
    )
    human.post_room_message(
        {
            "room_id": "reconcile-room",
            "task_id": "reconcile-offline",
            "subject": "Terminal offline seat",
            "body": "An unavailable seat must not leave an active prompt forever.",
        }
    )
    human.leave_room({"room_id": "reconcile-room", "agent_id": "alpha"})
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    receipt = coordinator.reconcile_message_dispatches({})
    assert receipt["count"] == 1
    assert receipt["reconciled"][0]["agent_id"] == "alpha"
    assert receipt["reconciled"][0]["error_code"] == "room_seat_unavailable"
    with sqlite3.connect(human.db_path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    noop = coordinator.reconcile_message_dispatches({})
    assert noop == {"reconciled": [], "count": 0, "audit_chain_sha256": None}
    with sqlite3.connect(human.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_coordinator_reconciles_exhausted_direct_route_with_configured_limit(
    tmp_path: Path,
) -> None:
    sender = make_bridge(tmp_path, "sender")
    worker = make_bridge(
        tmp_path,
        "worker",
        session="worker-session",
        provider_id="relay-main",
        model_id="model-a",
        reasoning_mode="high",
        route_class="relay",
    )
    message = sender.send_message(
        {
            "recipient": "worker",
            "task_id": "direct-exhausted",
            "subject": "Terminal direct route",
            "body": "Do not remain pending after the configured attempt limit.",
            "requested_provider_id": "relay-main",
            "requested_model_id": "model-a",
            "requested_reasoning_mode": "high",
            "requested_route_class": "relay",
        }
    )
    claim = worker.claim_message_dispatch(
        {"message_id": message["message_id"], "max_attempts": 1}
    )
    assert claim["claimed"] is True
    with sqlite3.connect(worker.db_path) as connection:
        connection.execute(
            "UPDATE message_dispatches SET lease_expires_epoch=0 WHERE message_id=?",
            (message["message_id"],),
        )

    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    receipt = coordinator.reconcile_message_dispatches(
        {"max_attempts": 1, "limit": 10}
    )

    assert receipt["count"] == 1
    assert receipt["reconciled"][0]["message_id"] == message["message_id"]
    assert receipt["reconciled"][0]["error_code"] == "dispatch_attempts_exhausted"
    with sqlite3.connect(worker.db_path) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM message_dispatches WHERE message_id=?",
            (message["message_id"],),
        ).fetchone() == ("failed", "dispatch_attempts_exhausted")


def test_coordinator_binds_runtime_observation_to_pending_route_sha(
    tmp_path: Path,
) -> None:
    sender = make_bridge(tmp_path, "sender")
    message = sender.send_message(
        {
            "recipient": "worker",
            "task_id": "runtime-observation",
            "subject": "No matching runtime",
            "body": "Record a bounded terminal reason.",
            "requested_provider_id": "relay-main",
            "requested_model_id": "model-a",
            "requested_reasoning_mode": "high",
            "requested_route_class": "relay",
        }
    )
    coordinator = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    with sqlite3.connect(sender.db_path) as connection:
        route_request_sha256 = connection.execute(
            "SELECT route_request_sha256 FROM messages WHERE message_id=?",
            (message["message_id"],),
        ).fetchone()[0]
    with pytest.raises(BridgeError, match="does not match the message route SHA"):
        coordinator.reconcile_message_dispatches(
            {
                "route_runtime_observations": [
                    {
                        "message_id": message["message_id"],
                        "route_request_sha256": "0" * 64,
                        "match_count": 0,
                    }
                ]
            }
        )

    receipt = coordinator.reconcile_message_dispatches(
        {
            "route_runtime_observations": [
                {
                    "message_id": message["message_id"],
                    "route_request_sha256": route_request_sha256,
                    "match_count": 0,
                }
            ]
        }
    )

    assert receipt["count"] == 1
    assert receipt["reconciled"][0]["error_code"] == "route_runtime_unavailable"


def test_only_coordinator_can_advance_discussions(tmp_path: Path) -> None:
    human, alpha, _beta = _automated_room(tmp_path)
    with pytest.raises(BridgeError, match="only the discussion coordinator"):
        alpha.advance_discussions({})
    with pytest.raises(BridgeError, match="only the discussion coordinator"):
        human.advance_discussions({})
    spoofed_supervisor = make_bridge(tmp_path, "mailbox-supervisor")
    with pytest.raises(BridgeError, match="only the discussion coordinator"):
        spoofed_supervisor.advance_discussions({})
    authorized_supervisor = make_bridge(
        tmp_path, "mailbox-supervisor", discussion_coordinator=True
    )
    assert authorized_supervisor.advance_discussions({})["count"] == 0


def test_agent_can_fanout_without_reply_cascade(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "agent-room", "name": "Agent room"})
    profiles = (
        ("alpha", "alpha-route", "relay-alpha", "model-alpha"),
        ("beta", "beta-route", "relay-beta", "model-beta"),
        ("gamma", "gamma-route", "relay-gamma", "model-gamma"),
    )
    for agent, route, provider, model in profiles:
        human.upsert_route_profile(
            {
                "route_id": route,
                "agent_id": agent,
                "provider_id": provider,
                "model_id": model,
                "route_class": "relay",
            }
        )
        human.join_room(
            {
                "room_id": "agent-room",
                "agent_id": agent,
                "route_profile_id": route,
            }
        )

    alpha = make_bridge(
        tmp_path,
        "alpha",
        provider_id="relay-alpha",
        model_id="model-alpha",
        route_class="relay",
    )
    fanout = alpha.post_room_message(
        {
            "room_id": "agent-room",
            "task_id": "agent-originated",
            "subject": "Peer question",
            "body": "Answer this once.",
        }
    )
    assert fanout["automation_mode"] == "once"
    assert [row["agent_id"] for row in fanout["recipients"]] == ["beta", "gamma"]

    for agent, _, provider, model in profiles[1:]:
        worker = make_bridge(
            tmp_path,
            agent,
            provider_id=provider,
            model_id=model,
            route_class="relay",
        )
        claim = worker.claim_message_dispatch(
            {"room_id": "agent-room", "route_profile_id": f"{agent}-route"}
        )
        assert claim["claimed"] is True
        worker.complete_message_dispatch(
            {
                "message_id": claim["message"]["message_id"],
                "lease_token": claim["lease_token"],
                "body": f"{agent} answer",
                "inference_receipt_sha256": stable_sha256(
                    {"agent_id": agent, "task_id": "agent-originated"}
                ),
            }
        )

    with sqlite3.connect(alpha.db_path) as connection:
        rows = connection.execute(
            """SELECT sender, recipient, reply_to, route_request_sha256
                 FROM messages WHERE task_id='agent-originated' ORDER BY sequence"""
        ).fetchall()
        fanout_events = connection.execute(
            """SELECT COUNT(*) FROM events
                 WHERE task_id='agent-originated'
                   AND event_type='message.room_fanout_sent'"""
        ).fetchone()[0]
        dispatches = connection.execute(
            """SELECT COUNT(*) FROM message_dispatches d
                 JOIN messages m ON m.scope=d.scope AND m.message_id=d.message_id
                WHERE m.task_id='agent-originated'"""
        ).fetchone()[0]

    assert rows == [
        ("alpha", "beta", None, rows[0][3]),
        ("alpha", "gamma", None, rows[1][3]),
        ("beta", "alpha", fanout["recipients"][0]["message_id"], None),
        ("gamma", "alpha", fanout["recipients"][1]["message_id"], None),
    ]
    assert rows[0][3] and rows[1][3]
    assert fanout_events == 1
    assert dispatches == 2
    assert alpha.claim_message_dispatch({"room_id": "agent-room"})["claimed"] is False


def test_only_room_manager_can_add_or_remove_other_agent_seats(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "managed", "name": "Managed"})
    human.join_room({"room_id": "managed", "agent_id": "peer"})
    peer = make_bridge(tmp_path, "peer")
    with pytest.raises(BridgeError, match="room creator or human operator"):
        peer.join_room({"room_id": "managed", "agent_id": "intruder"})
    with pytest.raises(BridgeError, match="room creator or human operator"):
        peer.leave_room({"room_id": "managed", "agent_id": "human-operator"})
    own_exit = peer.leave_room({"room_id": "managed"})
    assert own_exit["agent_id"] == "peer"
    assert own_exit["status"] == "left"


def test_memory_ledger_is_provider_neutral_but_visibility_isolated(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "alpha", "name": "Alpha"})
    human.create_room({"room_id": "beta", "name": "Beta"})
    human.join_room({"room_id": "alpha", "agent_id": "grok"})
    human.join_room({"room_id": "beta", "agent_id": "grok"})
    human.join_room({"room_id": "alpha", "agent_id": "claude"})
    alpha_message = human.send_message(
        {
            "room_id": "alpha",
            "recipient": "*",
            "task_id": "memory-task",
            "subject": "Alpha finding",
            "body": "The audited Alpha constraint is source-bound.",
        }
    )

    grok = make_bridge(tmp_path, "grok", provider_id="relay-grok", model_id="grok")
    room_memory = grok.record_memory(
        {
            "visibility": "room",
            "room_id": "alpha",
            "title": "Alpha constraint",
            "body": "Use only the source-bound Alpha constraint.",
            "source_message_id": alpha_message["message_id"],
        }
    )
    private_memory = grok.record_memory(
        {
            "visibility": "private",
            "room_id": "alpha",
            "title": "Grok scratch summary",
            "body": "This explicit summary remains owner-only.",
        }
    )
    assert room_memory["source_message_sha256"] == alpha_message["content_sha256"]

    claude = make_bridge(tmp_path, "claude", provider_id="anthropic", model_id="sonnet")
    visible_to_claude = claude.list_memories({"room_id": "alpha"})["memories"]
    assert [item["memory_id"] for item in visible_to_claude] == [
        room_memory["memory_id"]
    ]
    with pytest.raises(BridgeError, match="private memory"):
        claude.read_memory({"memory_id": private_memory["memory_id"]})

    beta_view = grok.list_memories({"room_id": "beta"})["memories"]
    assert room_memory["memory_id"] not in {
        item["memory_id"] for item in beta_view
    }
    assert private_memory["memory_id"] not in {
        item["memory_id"] for item in beta_view
    }


def test_only_human_can_publish_project_memory_and_cross_room_source_is_explicit(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "alpha", "name": "Alpha"})
    human.create_room({"room_id": "beta", "name": "Beta"})
    human.join_room({"room_id": "alpha", "agent_id": "grok"})
    human.join_room({"room_id": "beta", "agent_id": "grok"})
    source = human.send_message(
        {
            "room_id": "alpha",
            "recipient": "grok",
            "task_id": "publish-task",
            "subject": "Candidate memory",
            "body": "A human must approve any project-wide promotion.",
        }
    )
    grok = make_bridge(tmp_path, "grok")
    with pytest.raises(BridgeError, match="only human-operator"):
        grok.record_memory(
            {
                "visibility": "project",
                "title": "Unauthorized global memory",
                "body": "This must fail closed.",
                "source_message_id": source["message_id"],
            }
        )
    with pytest.raises(BridgeError, match="same room"):
        grok.record_memory(
            {
                "visibility": "room",
                "room_id": "beta",
                "title": "Wrong-room copy",
                "body": "This must not silently cross rooms.",
                "source_message_id": source["message_id"],
            }
        )

    project = human.record_memory(
        {
            "visibility": "project",
            "title": "Approved project constraint",
            "body": "This source-bound fact is approved for all providers.",
            "source_message_id": source["message_id"],
        }
    )
    assert project["source_message_sha256"] == source["content_sha256"]
    assert grok.read_memory({"memory_id": project["memory_id"]})[
        "visibility"
    ] == "project"

    revoked = human.revoke_memory(
        {"memory_id": project["memory_id"], "reason": "Superseded by a newer fact."}
    )
    assert revoked["status"] == "revoked"
    assert grok.list_memories({"visibility": "project"})["count"] == 0
    history = grok.list_memories(
        {"visibility": "project", "include_revoked": True}
    )["memories"]
    assert history[0]["status"] == "revoked"
    assert history[0]["revocation_sha256"] == revoked["revocation_sha256"]


def test_read_only_monitor_snapshot_includes_memory_ledger(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    (tmp_path / "evidence.txt").write_text("evidence", encoding="utf-8")
    memory = human.record_memory(
        {
            "visibility": "project",
            "title": "Monitor-visible memory",
            "body": "The control room can inspect this approved record.",
            "artifact_paths": ["evidence.txt"],
        }
    )

    snapshot = BridgeReader(human.db_path).snapshot(scope="test-scope")

    assert snapshot.table_counts["memories"] == 1
    assert snapshot.memories[0]["memory_id"] == memory["memory_id"]
    assert snapshot.memories[0]["memory_sha256"] == memory["memory_sha256"]


def test_read_only_monitor_snapshot_aggregates_real_usage_without_estimates(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    worker = make_bridge(
        tmp_path,
        "usage-worker",
        provider_id="provider-one",
        model_id="model-one",
        reasoning_mode="high",
        route_class="relay",
    )
    sent = human.send_message(
        {
            "recipient": "usage-worker",
            "task_id": "usage-monitor",
            "subject": "Measure real usage",
            "body": "Record only reported tokens.",
        }
    )
    claim = worker.claim_message_dispatch({})
    worker.complete_message_dispatch(
        {
            "message_id": sent["message_id"],
            "lease_token": claim["lease_token"],
            "body": "Measured reply.",
            "inference_receipt_sha256": "b" * 64,
            "inference_usage": {
                "schema": "peerbridge.inference-usage.v1",
                "status": "reported",
                "source": "test/provider",
                "input_tokens": 40,
                "output_tokens": 10,
                "total_tokens": 50,
                "cached_input_tokens": 8,
                "reasoning_tokens": 4,
                "reported_calls": 1,
                "total_calls": 1,
                "total_tokens_derived": False,
            },
        }
    )

    snapshot = BridgeReader(human.db_path).snapshot(scope="test-scope")

    assert snapshot.table_counts["inference_usage"] == 1
    assert snapshot.usage_totals["total_tokens"] == 50
    assert snapshot.usage_totals["reported_calls"] == 1
    assert snapshot.usage_totals["provider_calls"] == 1
    assert snapshot.usage_by_provider[0]["provider_id"] == "provider-one"
    assert snapshot.usage_by_provider[0]["total_tokens"] == 50
    assert snapshot.usage_by_provider[0]["today_tokens"] == 50
    assert snapshot.usage_by_model[0]["provider_id"] == "provider-one"
    assert snapshot.usage_by_model[0]["model_id"] == "model-one"
    assert len(snapshot.usage_daily) == 30
    assert snapshot.usage_daily[-1]["cached_input_tokens"] == 8
    assert snapshot.usage_daily[-1]["reasoning_tokens"] == 4
    assert snapshot.usage_model_totals[0]["model_id"] == "model-one"
    assert snapshot.usage_model_totals[0]["total_tokens"] == 50
    assert snapshot.usage_recent[0]["usage_status"] == "reported"


def test_monitor_usage_chart_merges_duplicate_models_and_zero_fills_calendar_days(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    workers = (
        make_bridge(
            tmp_path,
            "usage-worker-one",
            provider_id="provider-one",
            model_id="shared-model",
            route_class="relay",
        ),
        make_bridge(
            tmp_path,
            "usage-worker-two",
            provider_id="provider-two",
            model_id="shared-model",
            route_class="official",
        ),
    )
    for index, worker in enumerate(workers, start=1):
        sent = human.send_message(
            {
                "recipient": worker.agent_id,
                "task_id": f"usage-model-{index}",
                "subject": "Aggregate model usage",
                "body": "Keep route detail separate from the model chart.",
            }
        )
        claim = worker.claim_message_dispatch({})
        worker.complete_message_dispatch(
            {
                "message_id": sent["message_id"],
                "lease_token": claim["lease_token"],
                "body": "Measured reply.",
                "inference_receipt_sha256": str(index) * 64,
                "inference_usage": {
                    "schema": "peerbridge.inference-usage.v1",
                    "status": "reported",
                    "source": "test/provider",
                    "input_tokens": 10 * index,
                    "output_tokens": 5 * index,
                    "total_tokens": 15 * index,
                    "cached_input_tokens": 2 * index,
                    "reasoning_tokens": index,
                    "reported_calls": 1,
                    "total_calls": 1,
                    "total_tokens_derived": False,
                },
            }
        )

    snapshot = BridgeReader(human.db_path).snapshot(scope="test-scope")

    assert len(snapshot.usage_by_model) == 2
    assert snapshot.usage_model_totals == (
        {
            "model_id": "shared-model",
            "completed_dispatches": 2,
            "provider_calls": 2,
            "reported_calls": 2,
            "input_tokens": 30,
            "output_tokens": 15,
            "total_tokens": 45,
            "cached_input_tokens": 6,
            "reasoning_tokens": 3,
            "input_tokens_reported_calls": 2,
            "output_tokens_reported_calls": 2,
            "total_tokens_reported_calls": 2,
            "cached_input_tokens_reported_calls": 2,
            "reasoning_tokens_reported_calls": 2,
        },
    )
    assert len(snapshot.usage_daily) == 30
    assert sum(int(row["total_tokens"]) for row in snapshot.usage_daily) == 45

    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE inference_usage SET recorded_utc='2020-01-01T00:00:00Z'"
        )
        connection.commit()
    old_snapshot = BridgeReader(human.db_path).snapshot(scope="test-scope")
    assert len(old_snapshot.usage_daily) == 30
    assert sum(int(row["total_tokens"]) for row in old_snapshot.usage_daily) == 0


def test_monitor_usage_aggregates_preserve_unknown_component_values(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    workers = tuple(
        make_bridge(
            tmp_path,
            f"usage-worker-{index}",
            provider_id="provider-one",
            model_id="shared-model",
            route_class="relay",
        )
        for index in range(2)
    )
    message_ids: list[str] = []
    for index, worker in enumerate(workers):
        sent = human.send_message(
            {
                "recipient": worker.agent_id,
                "task_id": f"usage-unknown-{index}",
                "subject": "Preserve unknown usage components",
                "body": "Do not invent missing provider fields.",
            }
        )
        message_ids.append(sent["message_id"])
        claim = worker.claim_message_dispatch({})
        worker.complete_message_dispatch(
            {
                "message_id": sent["message_id"],
                "lease_token": claim["lease_token"],
                "body": "Measured reply.",
                "inference_receipt_sha256": str(index + 1) * 64,
                "inference_usage": {
                    "schema": "peerbridge.inference-usage.v1",
                    "status": "reported",
                    "source": "test/provider",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cached_input_tokens": 2,
                    "reasoning_tokens": 1,
                    "reported_calls": 1,
                    "total_calls": 1,
                    "total_tokens_derived": False,
                },
            }
        )

    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE inference_usage "
            "SET cached_input_tokens=NULL, reasoning_tokens=NULL, "
            "cached_input_tokens_reported_calls=0, "
            "reasoning_tokens_reported_calls=0 "
            "WHERE message_id=?",
            (message_ids[-1],),
        )
        connection.commit()

    snapshot = BridgeReader(human.db_path).snapshot(scope="test-scope")

    assert snapshot.usage_totals["input_tokens"] == 20
    assert snapshot.usage_totals["cached_input_tokens"] == 2
    assert snapshot.usage_totals["cached_input_tokens_reported_calls"] == 1
    assert snapshot.usage_totals["reasoning_tokens"] == 1
    assert snapshot.usage_totals["reasoning_tokens_reported_calls"] == 1
    assert snapshot.usage_by_model[0]["cached_input_tokens"] == 2
    assert snapshot.usage_by_model[0]["cached_input_tokens_reported_calls"] == 1
    assert snapshot.usage_model_totals[0]["reasoning_tokens"] == 1
    assert snapshot.usage_daily[-1]["cached_input_tokens"] == 2
    assert snapshot.usage_daily[-1]["reasoning_tokens"] == 1


def test_usage_trend_draws_four_chinese_series_including_zero_value_lines() -> None:
    class Locale:
        @staticmethod
        def get() -> str:
            return "zh-Hant"

    class RecordingCanvas:
        def __init__(self) -> None:
            self.lines: list[dict[str, object]] = []
            self.texts: list[str] = []

        @staticmethod
        def winfo_width() -> int:
            return 900

        @staticmethod
        def winfo_height() -> int:
            return 320

        @staticmethod
        def delete(_target: str) -> None:
            return None

        def create_line(self, *_args: object, **kwargs: object) -> None:
            self.lines.append(kwargs)

        def create_text(self, *_args: object, **kwargs: object) -> None:
            self.texts.append(str(kwargs.get("text") or ""))

        @staticmethod
        def create_oval(*_args: object, **_kwargs: object) -> None:
            return None

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.locale = Locale()
    canvas = RecordingCanvas()
    rows = (
        {
            "utc_date": "2026-08-16",
            "input_tokens": 10,
            "output_tokens": 6,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        },
        {
            "utc_date": "2026-08-17",
            "input_tokens": 20,
            "output_tokens": 12,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        },
    )

    monitor._draw_usage_trend_chart(canvas, rows)

    plotted_series = [line for line in canvas.lines if line.get("smooth") is True]
    assert len(plotted_series) == 4
    assert {"輸入", "輸出", "快取輸入", "推理"}.issubset(set(canvas.texts))
    rendered = " ".join(canvas.texts).lower()
    assert "cache" not in rendered
    assert "reason" not in rendered


def test_usage_provider_chart_keeps_all_twelve_provider_rows_visible() -> None:
    class Locale:
        @staticmethod
        def get() -> str:
            return "zh-Hant"

    class RecordingCanvas:
        def __init__(self) -> None:
            self.texts: list[str] = []

        @staticmethod
        def winfo_width() -> int:
            return 900

        @staticmethod
        def winfo_height() -> int:
            return 220

        @staticmethod
        def delete(_target: str) -> None:
            return None

        @staticmethod
        def create_rectangle(*_args: object, **_kwargs: object) -> None:
            return None

        def create_text(self, *_args: object, **kwargs: object) -> None:
            self.texts.append(str(kwargs.get("text") or ""))

    monitor = PixelMonitor.__new__(PixelMonitor)
    monitor.locale = Locale()
    canvas = RecordingCanvas()
    rows = tuple(
        {
            "provider_id": f"provider-{index:02d}",
            "provider_calls": index + 1,
            "total_tokens": (index + 1) * 100,
        }
        for index in range(12)
    )

    monitor._draw_usage_bar_chart(
        canvas,
        rows,
        label_key="provider_id",
        limit=12,
        orientation="horizontal",
    )

    for index in range(12):
        assert f"provider-{index:02d}" in canvas.texts


def test_monitor_room_view_repeated_refresh_is_bounded_and_write_free(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "control", "name": "Control Room"})
    sent = human.send_message(
        {
            "room_id": "control",
            "recipient": "*",
            "task_id": "monitor-refresh",
            "subject": "Read-only monitor evidence",
            "body": "Repeated UI refreshes must not create coordination events.",
        }
    )
    reader = BridgeReader(human.db_path)
    tracked_tables = (
        "events",
        "agent_presence",
        "consumer_cursors",
        "message_receipts",
        "message_route_receipts",
        "messages",
    )

    def table_counts() -> dict[str, int]:
        with sqlite3.connect(human.db_path) as connection:
            return {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tracked_tables
            }

    before_counts = table_counts()
    before_stat = human.db_path.stat()
    last_view: dict[str, object] | None = None
    for _ in range(250):
        last_view = reader.room_view(
            scope="test-scope",
            requested_room_id="control",
            consumer="human-operator",
            limit=25,
        )

    assert last_view is not None
    assert last_view["room_id"] == "control"
    assert last_view["operator_active"] is True
    assert [row["message_id"] for row in last_view["messages"]] == [sent["message_id"]]
    assert last_view["automation"]["mode"] == "once"
    assert last_view["automation"]["active_discussion"] is None
    assert table_counts() == before_counts
    after_stat = human.db_path.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_monitor_room_view_shows_complete_peer_to_peer_room_transcript(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "team-room", "name": "Team Room"})
    human.join_room({"room_id": "team-room", "agent_id": "peer-a"})
    human.join_room({"room_id": "team-room", "agent_id": "peer-b"})
    peer_a = make_bridge(tmp_path, "peer-a")
    sent = peer_a.send_message(
        {
            "room_id": "team-room",
            "recipient": "peer-b",
            "task_id": "peer-dialogue",
            "subject": "Review this",
            "body": "This peer-to-peer message must remain visible to the operator.",
        }
    )

    reader = BridgeReader(human.db_path)
    view = reader.room_view(
        scope="test-scope",
        requested_room_id="team-room",
        consumer="human-operator",
    )

    assert view["operator_active"] is True
    assert [row["message_id"] for row in view["messages"]] == [sent["message_id"]]
    assert view["messages"][0]["sender"] == "peer-a"
    assert view["messages"][0]["recipient"] == "peer-b"
    reader.close()


def test_monitor_room_history_pages_are_bounded_complete_and_non_overlapping(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "long-room", "name": "Long Room"})
    sent = [
        human.send_message(
            {
                "room_id": "long-room",
                "recipient": "*",
                "task_id": "bounded-history",
                "subject": f"Message {index:03d}",
                "body": f"Bounded room message {index:03d}.",
            }
        )
        for index in range(135)
    ]
    expected = [row["message_id"] for row in sent]
    reader = BridgeReader(human.db_path)

    pages: list[list[str]] = []
    before_sequence: int | None = None
    while True:
        view = reader.room_view(
            scope="test-scope",
            requested_room_id="long-room",
            consumer="human-operator",
            limit=60,
            before_sequence=before_sequence,
        )
        page_ids = [row["message_id"] for row in view["messages"]]
        assert 1 <= len(page_ids) <= 60
        pages.append(page_ids)
        if not view["page"]["has_older"]:
            break
        before_sequence = int(view["page"]["oldest_sequence"])

    flattened_oldest_first = [
        message_id for page in reversed(pages) for message_id in page
    ]
    assert flattened_oldest_first == expected
    assert len(flattened_oldest_first) == len(set(flattened_oldest_first)) == 135
    assert [len(page) for page in pages] == [60, 60, 15]
    reader.close()


def test_monitor_change_token_tracks_wal_writes_and_readers_are_stable(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    reader = BridgeReader(human.db_path)
    initial = reader.change_token()
    assert reader.change_token() == initial

    human.send_message(
        {
            "recipient": "*",
            "task_id": "change-token",
            "subject": "Database changed",
            "body": "The cheap UI token must observe this append-only write.",
        }
    )
    changed = reader.change_token()
    assert changed != initial

    reader.room_view(
        scope="test-scope",
        requested_room_id=DEFAULT_ROOM_ID,
    )
    assert reader.change_token() == changed


def test_monitor_room_view_keeps_history_readable_for_unseated_operator(
    tmp_path: Path,
) -> None:
    owner = make_bridge(tmp_path, "room-owner")
    owner.create_room({"room_id": "private-room", "name": "Private Room"})
    owner.send_message(
        {
            "room_id": "private-room",
            "recipient": "*",
            "task_id": "private-room-message",
            "subject": "Private room evidence",
            "body": "Local room history stays visible before control joins.",
        }
    )

    view = BridgeReader(owner.db_path).room_view(
        scope="test-scope",
        requested_room_id="private-room",
        consumer="human-operator",
    )

    assert view["room_id"] == "private-room"
    assert view["operator_active"] is False
    assert len(view["messages"]) == 1
    assert view["messages"][0]["body"] == (
        "Local room history stays visible before control joins."
    )
    assert view["read_error"] is None


def test_monitor_snapshot_enforces_human_memory_visibility(tmp_path: Path) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.create_room({"room_id": "shared", "name": "Shared"})
    human.join_room({"room_id": "shared", "agent_id": "grok"})

    grok = make_bridge(tmp_path, "grok")
    shared = grok.record_memory(
        {
            "visibility": "room",
            "room_id": "shared",
            "title": "Shared room fact",
            "body": "The human is an active member and may inspect this record.",
        }
    )
    grok_private = grok.record_memory(
        {
            "visibility": "private",
            "room_id": "shared",
            "title": "Grok private summary",
            "body": "The human control room must not display this record.",
        }
    )
    human_private = human.record_memory(
        {
            "visibility": "private",
            "room_id": "shared",
            "title": "Human private summary",
            "body": "The owning human may inspect this record.",
        }
    )

    isolated = make_bridge(tmp_path, "isolated-agent")
    isolated.create_room({"room_id": "isolated", "name": "Isolated"})
    isolated_room = isolated.record_memory(
        {
            "visibility": "room",
            "room_id": "isolated",
            "title": "Isolated room fact",
            "body": "The human is not a member and must not see this record.",
        }
    )
    (tmp_path / "approved.txt").write_text("approved", encoding="utf-8")
    project = human.record_memory(
        {
            "visibility": "project",
            "title": "Approved project fact",
            "body": "Every project participant may read this approved record.",
            "artifact_paths": ["approved.txt"],
        }
    )

    snapshot = BridgeReader(human.db_path).snapshot(scope="test-scope")
    visible_ids = {row["memory_id"] for row in snapshot.memories}

    assert visible_ids == {
        shared["memory_id"],
        human_private["memory_id"],
        project["memory_id"],
    }
    assert grok_private["memory_id"] not in visible_ids
    assert isolated_room["memory_id"] not in visible_ids
    assert snapshot.table_counts["memories"] == 3


def test_model_route_is_requested_then_verified_by_observed_runtime(tmp_path: Path) -> None:
    sender = make_bridge(tmp_path, "human-operator")
    profile = sender.upsert_route_profile(
        {
            "route_id": "claude-sol-max",
            "agent_id": "claude-code",
            "provider_id": "anthropic-official",
            "model_id": "SOL",
            "reasoning_mode": "max",
            "route_class": "official",
        }
    )
    assert profile["route_class"] == "official"
    assert sender.list_route_profiles({})["profiles"][0]["route_id"] == "claude-sol-max"
    sent = sender.send_message(
        {
            "recipient": "claude-code",
            "task_id": "routed-work",
            "subject": "USE SOL",
            "body": "Review this with the selected route.",
            "route_profile_id": "claude-sol-max",
        }
    )
    assert sent["route_status"] == "requested"
    assert sent["route_request"]["requested_model_id"] == "SOL"
    assert sent["route_request"]["requested_route_class"] == "official"
    assert sent["route_request"]["route_profile_sha256"] == profile["profile_sha256"]
    route_request_content = {
        key: value
        for key, value in sent["route_request"].items()
        if key != "route_request_sha256"
    }
    assert sent["route_request"]["route_request_sha256"] == stable_sha256(
        route_request_content
    )
    assert sent["content_sha256"] == stable_sha256(
        {
            "message_id": sent["message_id"],
            "scope": "test-scope",
            "room_id": DEFAULT_ROOM_ID,
            "task_id": "routed-work",
            "sender": "human-operator",
            "recipient": "claude-code",
            "subject": "USE SOL",
            "body": "Review this with the selected route.",
            "priority": "normal",
            "reply_to": None,
            "artifact_paths": [],
            "route_request": Bridge._route_request_content_binding(
                sent["route_request"]
            ),
            "created_utc": sent["created_utc"],
        }
    )
    with sqlite3.connect(sender.db_path) as connection:
        stored_profile_sha256 = connection.execute(
            "SELECT route_profile_sha256 FROM messages WHERE message_id=?",
            (sent["message_id"],),
        ).fetchone()[0]
    assert stored_profile_sha256 == profile["profile_sha256"]

    wrong = make_bridge(
        tmp_path,
        "claude-code",
        session="wrong-session",
        provider_id="anthropic-official",
        model_id="LUNA",
        reasoning_mode="max",
        route_class="official",
    )
    wrong_message = wrong.poll_messages({})["messages"][0]
    assert wrong_message["route_profile_sha256"] == profile["profile_sha256"]
    assert (
        wrong_message["route_request"]["route_profile_sha256"]
        == profile["profile_sha256"]
    )
    assert wrong_message["route_evaluation"]["status"] == "mismatch"
    assert wrong_message["route_evaluation"]["mismatches"] == ["model_id"]
    with pytest.raises(BridgeError, match="route request is not satisfied"):
        wrong.ack_message({"message_id": sent["message_id"]})

    correct = make_bridge(
        tmp_path,
        "claude-code",
        session="correct-session",
        provider_id="anthropic-official",
        model_id="SOL",
        reasoning_mode="max",
        route_class="official",
    )
    receipt = correct.ack_message({"message_id": sent["message_id"]})
    assert receipt["route_evaluation"]["status"] == "verified"
    assert receipt["route_receipt"]["observed_model_id"] == "SOL"
    assert receipt["route_receipt"]["observed_route_class"] == "official"
    route_receipt_content = {
        "scope": "test-scope",
        "message_id": sent["message_id"],
        "agent_id": "claude-code",
        "session_id": "correct-session",
        "observed_provider_id": "anthropic-official",
        "observed_model_id": "SOL",
        "observed_reasoning_mode": "max",
        "observed_route_class": "official",
        "route_status": "verified",
        "acknowledged_utc": receipt["route_receipt"]["acknowledged_utc"],
        "route_request_sha256": sent["route_request"]["route_request_sha256"],
    }
    assert receipt["route_receipt"]["receipt_sha256"] == stable_sha256(
        route_receipt_content
    )

    with correct._connect() as connection:
        stored = connection.execute(
            """SELECT route_status, observed_model_id, observed_route_class
               FROM message_route_receipts"""
        ).fetchone()
    assert tuple(stored) == ("verified", "SOL", "official")


def test_route_profile_sha_tamper_fails_closed_after_all_message_hashes_change(
    tmp_path: Path,
) -> None:
    sender = make_bridge(tmp_path, "human-operator")
    profile = sender.upsert_route_profile(
        {
            "route_id": "tamper-route",
            "agent_id": "worker",
            "provider_id": "relay-main",
            "model_id": "model-a",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    sent = sender.send_message(
        {
            "recipient": "worker",
            "task_id": "tamper-route",
            "subject": "Bound route",
            "body": "Changing every message-local hash must still fail closed.",
            "route_profile_id": "tamper-route",
        }
    )
    tampered_profile_sha256 = "f" * 64
    assert tampered_profile_sha256 != profile["profile_sha256"]
    tampered_request_content = {
        key: value
        for key, value in sent["route_request"].items()
        if key != "route_request_sha256"
    }
    tampered_request_content["route_profile_sha256"] = tampered_profile_sha256
    tampered_request = {
        **tampered_request_content,
        "route_request_sha256": stable_sha256(tampered_request_content),
    }
    tampered_content = {
        "message_id": sent["message_id"],
        "scope": "test-scope",
        "room_id": DEFAULT_ROOM_ID,
        "task_id": "tamper-route",
        "sender": "human-operator",
        "recipient": "worker",
        "subject": "Bound route",
        "body": "Changing every message-local hash must still fail closed.",
        "priority": "normal",
        "reply_to": None,
        "artifact_paths": [],
        "route_request": Bridge._route_request_content_binding(tampered_request),
        "created_utc": sent["created_utc"],
    }
    with sqlite3.connect(sender.db_path) as connection:
        connection.execute(
            """UPDATE messages
                  SET route_profile_sha256=?, route_request_sha256=?, content_sha256=?
                WHERE message_id=?""",
            (
                tampered_profile_sha256,
                tampered_request["route_request_sha256"],
                stable_sha256(tampered_content),
                sent["message_id"],
            ),
        )

    worker = make_bridge(
        tmp_path,
        "worker",
        provider_id="relay-main",
        model_id="model-a",
        reasoning_mode="high",
        route_class="relay",
    )
    message = worker.poll_messages({})["messages"][0]
    assert message["route_evaluation"]["status"] == "mismatch"
    assert message["route_evaluation"]["mismatches"] == [
        "route_profile_sha256"
    ]
    with pytest.raises(BridgeError, match="route_profile_sha256"):
        worker.claim_message_dispatch(
            {
                "message_id": sent["message_id"],
                "route_profile_id": "tamper-route",
            }
        )
    with pytest.raises(BridgeError, match="route_profile_sha256"):
        worker.ack_message({"message_id": sent["message_id"]})


@pytest.mark.parametrize("requested_class", ["official", "relay", "local"])
def test_route_class_requires_matching_runtime_attestation(
    tmp_path: Path, requested_class: str
) -> None:
    sender = make_bridge(tmp_path, "human-operator")
    agent_id = f"worker-{requested_class}"
    sender.upsert_route_profile(
        {
            "route_id": f"route-{requested_class}",
            "agent_id": agent_id,
            "provider_id": "same-provider-label",
            "model_id": "same-model-label",
            "route_class": requested_class,
        }
    )
    sent = sender.send_message(
        {
            "recipient": agent_id,
            "task_id": f"task-{requested_class}",
            "subject": "class-bound route",
            "body": "Labels alone must not satisfy this route.",
            "route_profile_id": f"route-{requested_class}",
        }
    )

    labels_only = make_bridge(
        tmp_path,
        agent_id,
        session=f"labels-only-{requested_class}",
        provider_id="same-provider-label",
        model_id="same-model-label",
    )
    labels_only_evaluation = labels_only.poll_messages({})["messages"][0][
        "route_evaluation"
    ]
    assert labels_only_evaluation["status"] == "mismatch"
    assert labels_only_evaluation["mismatches"] == ["route_class"]
    with pytest.raises(BridgeError, match="route_class"):
        labels_only.ack_message({"message_id": sent["message_id"]})

    wrong_class = next(
        route_class
        for route_class in ("official", "relay", "local")
        if route_class != requested_class
    )
    mislabeled_runtime = make_bridge(
        tmp_path,
        agent_id,
        session=f"wrong-class-{requested_class}",
        provider_id="same-provider-label",
        model_id="same-model-label",
        route_class=wrong_class,
    )
    wrong_evaluation = mislabeled_runtime.poll_messages({})["messages"][0][
        "route_evaluation"
    ]
    assert wrong_evaluation["status"] == "mismatch"
    assert wrong_evaluation["mismatches"] == ["route_class"]
    with pytest.raises(BridgeError, match="route_class"):
        mislabeled_runtime.ack_message({"message_id": sent["message_id"]})

    matching_runtime = make_bridge(
        tmp_path,
        agent_id,
        session=f"matching-class-{requested_class}",
        provider_id="same-provider-label",
        model_id="same-model-label",
        route_class=requested_class,
    )
    acknowledged = matching_runtime.ack_message({"message_id": sent["message_id"]})
    assert acknowledged["route_evaluation"]["status"] == "verified"
    assert acknowledged["route_receipt"]["observed_route_class"] == requested_class


def test_provider_connection_registry_is_redacted_and_sha_bound(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "human-operator")
    endpoint_sha = "a" * 64
    credential_sha = "b" * 64
    result = bridge.upsert_provider_connection(
        {
            "connection_id": "relay-one",
            "display_name": "Relay One",
            "route_class": "relay",
            "provider_id": "relay-one",
            "secret_backend": "windows-credential-manager",
            "credential_target": credential_target("test-scope", "relay-one"),
            "endpoint_sha256": endpoint_sha,
            "credential_fingerprint_sha256": credential_sha,
            "descriptor_schema": "peerbridge.provider-credential.v2",
            "credential_version_sha256": credential_sha,
        }
    )

    assert result["endpoint_sha256"] == endpoint_sha
    listed = bridge.list_provider_connections({})["connections"]
    assert len(listed) == 1
    assert set(listed[0]).isdisjoint({"api_key", "endpoint", "base_url"})

    with sqlite3.connect(bridge.db_path) as connection:
        serialized = "\n".join(
            str(row[0]) for row in connection.execute("SELECT payload_json FROM events")
        )
    assert "https://" not in serialized
    assert "api_key" not in serialized


def test_provider_and_route_registry_enforce_actor_ownership_and_immutability(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    alpha = make_bridge(tmp_path, "alpha")
    beta = make_bridge(tmp_path, "beta")

    own = alpha.upsert_route_profile(
        {
            "route_id": "alpha-route-v1",
            "agent_id": "alpha",
            "provider_id": "relay-alpha",
            "model_id": "model-a",
            "route_class": "relay",
        }
    )
    assert own["agent_id"] == "alpha"
    with pytest.raises(BridgeError, match="route owner"):
        beta.upsert_route_profile(
            {
                "route_id": "alpha-route-v2",
                "agent_id": "alpha",
                "provider_id": "relay-alpha",
                "model_id": "model-b",
                "route_class": "relay",
            }
        )

    identical = human.upsert_route_profile(
        {
            "route_id": "alpha-route-v1",
            "agent_id": "alpha",
            "provider_id": "relay-alpha",
            "model_id": "model-a",
            "route_class": "relay",
        }
    )
    assert identical["profile_sha256"] == own["profile_sha256"]
    assert identical["updated_utc"] == own["updated_utc"]
    with pytest.raises(BridgeError, match="immutable"):
        human.upsert_route_profile(
            {
                "route_id": "alpha-route-v1",
                "agent_id": "alpha",
                "provider_id": "relay-alpha",
                "model_id": "model-b",
                "route_class": "relay",
            }
        )
    stored = human.list_route_profiles({"agent_id": "alpha"})["profiles"]
    assert [(row["route_id"], row["model_id"]) for row in stored] == [
        ("alpha-route-v1", "model-a")
    ]

    provider_args = {
        "connection_id": "relay-one",
        "display_name": "Relay One",
        "route_class": "relay",
        "provider_id": "relay-one",
        "secret_backend": "windows-credential-manager",
        "credential_target": credential_target("test-scope", "relay-one"),
        "endpoint_sha256": "a" * 64,
        "credential_fingerprint_sha256": "b" * 64,
        "descriptor_schema": "peerbridge.provider-credential.v2",
        "credential_version_sha256": "b" * 64,
    }
    with pytest.raises(BridgeError, match="human operator"):
        alpha.upsert_provider_connection(provider_args)
    assert human.upsert_provider_connection(provider_args)["connection_id"] == "relay-one"


def test_legacy_route_profile_hash_remains_valid_when_response_model_is_null(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    profile = human.upsert_route_profile(
        {
            "route_id": "legacy-route-v1",
            "agent_id": "reviewer",
            "client_name": "claude",
            "provider_id": "relay-claude",
            "model_id": "model-a",
            "route_class": "relay",
        }
    )
    legacy_identity = {
        "scope": "test-scope",
        "route_id": "legacy-route-v1",
        "agent_id": "reviewer",
        "client_name": "claude",
        "provider_id": "relay-claude",
        "model_id": "model-a",
        "reasoning_mode": None,
        "route_class": "relay",
        "enabled": True,
    }
    legacy_sha = stable_sha256(legacy_identity)
    assert legacy_sha != profile["profile_sha256"]
    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE route_profiles SET profile_sha256=? WHERE scope=? AND route_id=?",
            (legacy_sha, "test-scope", "legacy-route-v1"),
        )

    identical = human.upsert_route_profile(
        {
            "route_id": "legacy-route-v1",
            "agent_id": "reviewer",
            "client_name": "claude",
            "provider_id": "relay-claude",
            "model_id": "model-a",
            "route_class": "relay",
        }
    )
    assert identical["profile_sha256"] == legacy_sha
    sent = human.send_message(
        {
            "recipient": "reviewer",
            "subject": "legacy route",
            "body": "review",
            "task_id": "legacy-route-test",
            "route_profile_id": "legacy-route-v1",
        }
    )
    assert sent["route_request"]["route_profile_sha256"] == legacy_sha


def test_legacy_route_profile_hash_is_rejected_for_non_null_response_model(
    tmp_path: Path,
) -> None:
    human = make_bridge(tmp_path, "human-operator")
    human.upsert_route_profile(
        {
            "route_id": "response-route-v1",
            "agent_id": "reviewer",
            "client_name": "openai-compatible",
            "provider_id": "relay-reviewer",
            "model_id": "request-model",
            "response_model_id": "observed-model",
            "route_class": "relay",
        }
    )
    legacy_identity = {
        "scope": "test-scope",
        "route_id": "response-route-v1",
        "agent_id": "reviewer",
        "client_name": "openai-compatible",
        "provider_id": "relay-reviewer",
        "model_id": "request-model",
        "reasoning_mode": None,
        "route_class": "relay",
        "enabled": True,
    }
    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE route_profiles SET profile_sha256=? WHERE scope=? AND route_id=?",
            (stable_sha256(legacy_identity), "test-scope", "response-route-v1"),
        )

    with pytest.raises(BridgeError, match="route profile identity SHA mismatch"):
        human.send_message(
            {
                "recipient": "reviewer",
                "subject": "tampered response route",
                "body": "review",
                "task_id": "response-route-test",
                "route_profile_id": "response-route-v1",
            }
        )


def test_provider_connection_tool_schema_rejects_raw_secrets(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "human-operator")
    response = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "upsert_provider_connection",
                "arguments": {
                    "connection_id": "relay-one",
                    "display_name": "Relay One",
                    "route_class": "relay",
                    "secret_backend": "windows-credential-manager",
                    "credential_target": "PeerBridgeMCP:test-scope:relay-one",
                    "endpoint_sha256": "a" * 64,
                    "credential_fingerprint_sha256": "b" * 64,
                    "api_key": _test_credential("not", "allowed"),
                },
            },
        },
    )
    assert response["error"]["code"] == -32602
    assert "api_key" not in response["error"]["message"]


def test_model_route_rejects_broadcast_and_profile_conflicts(tmp_path: Path) -> None:
    sender = make_bridge(tmp_path, "human-operator")
    sender.upsert_route_profile(
        {
            "route_id": "codex-luna",
            "agent_id": "codex",
            "model_id": "LUNA",
            "route_class": "relay",
        }
    )
    with pytest.raises(BridgeError, match="explicit recipient"):
        sender.send_message(
            {
                "recipient": "*",
                "task_id": "bad-broadcast",
                "subject": "route",
                "body": "must not broadcast",
                "requested_model_id": "SOL",
            }
        )
    with pytest.raises(BridgeError, match="conflicts with route profile"):
        sender.send_message(
            {
                "recipient": "codex",
                "task_id": "bad-conflict",
                "subject": "route",
                "body": "must not override profile",
                "route_profile_id": "codex-luna",
                "requested_model_id": "SOL",
            }
        )
    with pytest.raises(BridgeError, match="require route_profile_id"):
        sender.send_message(
            {
                "recipient": "codex",
                "task_id": "labels-only",
                "subject": "route",
                "body": "labels alone must fail closed",
                "requested_model_id": "LUNA",
            }
        )


def test_task_leases_block_overlapping_writers_and_allow_expired_recovery(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    first = make_bridge(tmp_path, "first")
    second = make_bridge(tmp_path, "second")
    claim = first.claim_task(
        {"task_id": "alpha", "summary": "first", "write_paths": ["src"], "lease_seconds": 30}
    )

    with pytest.raises(BridgeError, match="conflicts"):
        second.claim_task(
            {"task_id": "beta", "summary": "second", "write_paths": ["src/module.py"]}
        )

    with first._connect() as connection:
        connection.execute(
            "UPDATE tasks SET lease_expires_epoch=? WHERE scope=? AND task_id=?",
            (time.time() - 1, first.scope, "alpha"),
        )
    recovered = second.claim_task(
        {"task_id": "beta", "summary": "second", "write_paths": ["src/module.py"]}
    )
    assert recovered["claimed_by"] == "second"
    assert recovered["lease_token"] != claim["lease_token"]


def test_task_leases_collide_across_scopes_for_the_same_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    db_path = tmp_path / ".peerbridge" / "peerbridge.sqlite3"
    first = Bridge(tmp_path, db_path, "first", "scope-a")
    second = Bridge(tmp_path, db_path, "second", "scope-b")
    first.claim_task(
        {"task_id": "writer-a", "summary": "first", "write_paths": ["src"]}
    )
    with pytest.raises(BridgeError, match="conflicts"):
        second.claim_task(
            {
                "task_id": "writer-b",
                "summary": "second",
                "write_paths": ["src/module.py"],
            }
        )


def test_concurrent_bridge_initialization_serializes_schema_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".peerbridge" / "peerbridge.sqlite3"

    def initialize(index: int) -> str:
        bridge = Bridge(tmp_path, db_path, f"agent-{index}", f"scope-{index % 3}")
        return str(bridge.status()["schema_version"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(initialize, range(24)))
    assert versions == ["17"] * 24
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "17"
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tasks)")
        }
    assert "workspace_root_key" in columns


def test_read_read_scopes_do_not_conflict_but_read_write_does(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    first = make_bridge(tmp_path, "first")
    second = make_bridge(tmp_path, "second")
    third = make_bridge(tmp_path, "third")
    first.claim_task({"task_id": "a", "summary": "read", "read_paths": ["docs"]})
    second.claim_task({"task_id": "b", "summary": "read", "read_paths": ["docs/guide.md"]})
    with pytest.raises(BridgeError, match="conflicts"):
        third.claim_task({"task_id": "c", "summary": "write", "write_paths": ["docs"]})


def test_proof_gate_rehashes_live_files_and_rejects_drift(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    target = source / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    agent = make_bridge(tmp_path, "agent")
    claim = agent.claim_task(
        {
            "task_id": "proof",
            "summary": "proof gate",
            "write_paths": ["src"],
            "approval_mode": "solo_allowed",
        }
    )
    agent.record_proof(
        {
            "task_id": "proof",
            "lease_token": claim["lease_token"],
            "change_summary": "changed module",
            "changed_paths": ["src/module.py"],
            "before_hashes": {
                "src/module.py": hashlib.sha256(target.read_bytes()).hexdigest()
            },
            "tests": "pytest: pass",
        }
    )
    target.write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(BridgeError, match="drift"):
        agent.complete_task({"task_id": "proof", "lease_token": claim["lease_token"]})


def test_record_proof_rejects_unbound_or_non_hash_before_values(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    agent = make_bridge(tmp_path, "agent")
    claim = agent.claim_task(
        {
            "task_id": "proof-input",
            "summary": "proof input validation",
            "write_paths": ["src"],
            "approval_mode": "solo_allowed",
        }
    )
    common = {
        "task_id": "proof-input",
        "lease_token": claim["lease_token"],
        "change_summary": "changed module",
        "changed_paths": ["src/module.py"],
        "tests": "pytest: pass",
    }

    with pytest.raises(BridgeError, match="exactly one entry"):
        agent.record_proof({**common, "before_hashes": {}})

    with pytest.raises(BridgeError, match="SHA-256"):
        agent.record_proof(
            {
                **common,
                "before_hashes": {"src/module.py": "sk-" + "A" * 24},
            }
        )
    with pytest.raises(BridgeError, match="changed_paths"):
        agent.record_proof(
            {
                **common,
                "before_hashes": {"src/other.py": "a" * 64},
            }
        )

    recorded = agent.record_proof(
        {**common, "before_hashes": {"src/module.py": None}}
    )
    assert recorded["record_id"]
    records = agent.change_log({"task_id": "proof-input"})["records"]
    assert records[0]["before_hashes"] == {"src/module.py": None}


def test_presence_aware_review_requires_online_peer_then_completes(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    target = source / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    owner = make_bridge(tmp_path, "owner")
    peer = make_bridge(tmp_path, "peer")
    owner.touch_presence()
    peer.touch_presence()
    claim = owner.claim_task(
        {
            "task_id": "reviewed",
            "summary": "needs peer",
            "write_paths": ["src"],
            "approval_mode": "presence_aware",
            "required_peer": "peer",
        }
    )
    owner.record_proof(
        {
            "task_id": "reviewed",
            "lease_token": claim["lease_token"],
            "change_summary": "reviewed change",
            "changed_paths": ["src/module.py"],
            "before_hashes": {
                "src/module.py": hashlib.sha256(target.read_bytes()).hexdigest()
            },
            "tests": "pytest: pass",
        }
    )
    with pytest.raises(BridgeError, match="review gate"):
        owner.complete_task({"task_id": "reviewed", "lease_token": claim["lease_token"]})
    request = owner.request_review(
        {
            "task_id": "reviewed",
            "lease_token": claim["lease_token"],
            "recipient": "peer",
            "question": "Review the bounded change and test evidence.",
        }
    )
    review = peer.submit_review(
        {
            "request_id": request["request_id"],
            "verdict": "approved",
            "score": 95,
            "findings": "The file and test evidence are internally consistent.",
        }
    )
    completed = owner.complete_task(
        {"task_id": "reviewed", "lease_token": claim["lease_token"]}
    )
    assert completed["status"] == "complete"
    assert review["review_id"] in {
        item["review_id"] for item in completed["review"]["reviews"]
    }


def test_presence_aware_mode_allows_traced_solo_fallback(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("checked\n", encoding="utf-8")
    owner = make_bridge(tmp_path, "owner")
    owner.touch_presence()
    claim = owner.claim_task(
        {
            "task_id": "solo",
            "summary": "offline fallback",
            "approval_mode": "presence_aware",
            "required_peer": "offline-peer",
        }
    )
    owner.record_proof(
        {
            "task_id": "solo",
            "lease_token": claim["lease_token"],
            "change_summary": "read-only evidence",
            "tests": "manual verifier: pass",
            "evidence_paths": ["evidence.txt"],
        }
    )
    result = owner.complete_task({"task_id": "solo", "lease_token": claim["lease_token"]})
    assert result["review"]["policy_reason"] == "peer_offline_solo_fallback"


def test_protected_paths_and_secret_values_fail_closed(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "agent")
    with pytest.raises(BridgeError, match="protected"):
        bridge.claim_task(
            {"task_id": "git", "summary": "bad", "write_paths": [".git/config"]}
        )
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=not-for-bridge\n", encoding="utf-8")
    with pytest.raises(BridgeError, match="protected"):
        bridge.hash_artifact({"path": ".env"})
    for sensitive_name in (
        ".env.local",
        ".env.production",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "id_rsa",
        "client.ovpn",
        "ordinary.txt:secret-stream",
        "CON.txt",
    ):
        with pytest.raises(BridgeError, match="protected"):
            bridge.hash_artifact({"path": sensitive_name})

    disguised = tmp_path / "ordinary.txt"
    disguised.write_text("api_key=" + "Q" * 32, encoding="utf-8")
    with pytest.raises(BridgeError, match="credential"):
        bridge.read_artifact({"path": "ordinary.txt"})
    with pytest.raises(BridgeError, match="credential"):
        bridge.send_message(
            {
                "recipient": "peer",
                "task_id": "secret",
                "subject": "bad",
                "body": "s" + "k-" + "a" * 40,
            }
        )

    generic_secret = "Z" * 24
    with pytest.raises(BridgeError, match="credential"):
        bridge.send_message(
            {
                "recipient": "peer",
                "task_id": "generic-secret",
                "subject": "bad",
                "body": "api_" + "key=" + generic_secret,
            }
        )
    database_parts = [bridge.db_path]
    database_parts.extend(
        path
        for path in (
            Path(f"{bridge.db_path}-wal"),
            Path(f"{bridge.db_path}-shm"),
        )
        if path.exists()
    )
    assert all(generic_secret.encode("utf-8") not in path.read_bytes() for path in database_parts)


def test_event_hash_chain_detects_payload_tampering(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "agent")
    bridge.send_message(
        {"recipient": "peer", "task_id": "audit", "subject": "hello", "body": "world"}
    )
    assert bridge.verify_audit_chain()["valid"] is True
    with bridge._connect() as connection:
        connection.execute(
            "UPDATE events SET payload_json=? WHERE sequence=(SELECT MIN(sequence) FROM events)",
            (json.dumps({"tampered": True}),),
        )
    result = bridge.verify_audit_chain()
    assert result["valid"] is False
    assert any(item["error"] == "payload_sha256" for item in result["errors"])


def test_mcp_initialize_list_and_tool_call(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "agent")
    initialized = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )
    assert initialized["result"]["serverInfo"]["name"] == "peerbridge-mcp"
    tools = handle_request(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {item["name"] for item in tools["result"]["tools"]}
    assert {"claim_task", "verify_audit_chain", "complete_task"}.issubset(names)
    assert {
        "post_room_message",
        "get_room_automation",
        "set_room_automation",
        "control_discussion",
        "advance_discussions",
        "reconcile_message_dispatches",
    }.issubset(names)
    called = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        },
    )
    payload = json.loads(called["result"]["content"][0]["text"])
    assert payload["network_listener"] is False


def test_mcp_tool_allowlist_filters_discovery_and_calls(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "limited-agent")
    allowed = frozenset({"bridge_status"})
    listed = handle_request(
        bridge,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        allowed,
    )
    assert [item["name"] for item in listed["result"]["tools"]] == ["bridge_status"]

    denied = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "send_message", "arguments": {}},
        },
        allowed,
    )
    assert denied["error"]["code"] == -32602
    assert denied["error"]["message"] == "Tool is not allowed: send_message"

    accepted = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        },
        allowed,
    )
    payload = json.loads(accepted["result"]["content"][0]["text"])
    assert payload["agent_id"] == "limited-agent"


def test_modern_mcp_discovery_versioning_and_structured_results(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "modern-agent")
    meta = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }
    discovered = handle_request(
        bridge,
        {"jsonrpc": "2.0", "id": "d1", "method": "server/discover", "params": meta},
    )
    assert discovered["result"]["resultType"] == "complete"
    assert "2026-07-28" in discovered["result"]["supportedVersions"]

    listed = handle_request(
        bridge,
        {"jsonrpc": "2.0", "id": "l1", "method": "tools/list", "params": meta},
    )
    assert listed["result"]["resultType"] == "complete"
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in listed["result"]["tools"])

    called = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": "c1",
            "method": "tools/call",
            "params": {**meta, "name": "bridge_status", "arguments": {}},
        },
    )
    assert called["result"]["resultType"] == "complete"
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["network_listener"] is False


def test_modern_mcp_errors_are_protocol_or_recoverable_tool_results(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "modern-agent")
    unsupported = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {
                "_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}
            },
        },
    )
    assert unsupported["error"]["code"] == -32022
    assert unsupported["error"]["data"]["requested"] == "1900-01-01"

    meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
    unknown = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {**meta, "name": "does_not_exist", "arguments": {}},
        },
    )
    assert unknown["error"]["code"] == -32602

    recoverable = handle_request(
        bridge,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {**meta, "name": "ack_message", "arguments": {}},
        },
    )
    assert recoverable["result"]["isError"] is True
    assert recoverable["result"]["resultType"] == "complete"


def test_multi_agent_quorum_requires_configured_number_of_reviews(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    owner = make_bridge(tmp_path, "codex")
    peers = {name: make_bridge(tmp_path, name) for name in ("grok", "deepseek", "kimi")}
    owner.touch_presence()
    for peer in peers.values():
        peer.touch_presence()

    claim = owner.claim_task(
        {
            "task_id": "multi-agent",
            "summary": "Require two independent peers",
            "write_paths": ["module.py"],
            "approval_mode": "quorum_required",
            "required_peers": list(peers),
            "review_quorum": 2,
        }
    )
    owner.record_proof(
        {
            "task_id": "multi-agent",
            "lease_token": claim["lease_token"],
            "change_summary": "Bounded synthetic change",
            "changed_paths": ["module.py"],
            "before_hashes": {
                "module.py": hashlib.sha256(
                    (tmp_path / "module.py").read_bytes()
                ).hexdigest()
            },
            "tests": "synthetic check: pass",
        }
    )
    requests = {
        name: owner.request_review(
            {
                "task_id": "multi-agent",
                "lease_token": claim["lease_token"],
                "recipient": name,
                "question": "Review the synthetic proof independently.",
            }
        )
        for name in peers
    }
    peers["grok"].submit_review(
        {
            "request_id": requests["grok"]["request_id"],
            "verdict": "approved",
            "score": 90,
            "findings": "Synthetic evidence and live hash agree.",
        }
    )
    with pytest.raises(BridgeError, match="review gate"):
        owner.complete_task({"task_id": "multi-agent", "lease_token": claim["lease_token"]})

    peers["deepseek"].submit_review(
        {
            "request_id": requests["deepseek"]["request_id"],
            "verdict": "approved",
            "score": 92,
            "findings": "Independent synthetic review also passes.",
        }
    )
    completed = owner.complete_task(
        {"task_id": "multi-agent", "lease_token": claim["lease_token"]}
    )
    assert completed["review"]["policy_reason"] == "review_quorum_met"
    assert completed["review"]["review_quorum"] == 2
    assert completed["review"]["required_peers"] == ["deepseek", "grok", "kimi"]


def test_runtime_identity_distinguishes_official_and_relay_routes(tmp_path: Path) -> None:
    official = make_bridge(
        tmp_path,
        "grok-official-session",
        session="official-session",
        client_name="browser-adapter",
        provider_id="xai-official-web",
        model_id="grok",
        route_class="official",
    )
    relay = make_bridge(
        tmp_path,
        "grok-relay-session",
        session="relay-session",
        client_name="relay-coding-client",
        provider_id="relay:grok-official-channel",
        model_id="grok",
        route_class="relay",
    )
    official.touch_presence()
    relay.touch_presence()

    sessions = {
        row["agent_id"]: row for row in official.presence_snapshot()["online_sessions"]
    }
    assert sessions["grok-official-session"]["provider_id"] == "xai-official-web"
    assert sessions["grok-relay-session"]["provider_id"] == "relay:grok-official-channel"
    assert sessions["grok-official-session"]["route_class"] == "official"
    assert sessions["grok-relay-session"]["route_class"] == "relay"
    assert official.status()["runtime_identity"] == {
        "client_name": "browser-adapter",
        "provider_id": "xai-official-web",
        "model_id": "grok",
        "reasoning_mode": None,
        "route_class": "official",
    }


def test_schema_v1_database_migrates_additively_to_v17(tmp_path: Path) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '1');
            CREATE TABLE tasks (
                scope TEXT NOT NULL,
                task_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                owner TEXT,
                status TEXT NOT NULL,
                claimed_by TEXT,
                claimed_session_id TEXT,
                lease_token_sha256 TEXT,
                lease_expires_epoch REAL,
                claimed_utc TEXT,
                created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL,
                task_sha256 TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                required_peer TEXT,
                PRIMARY KEY(scope, task_id)
            );
            """
        )
    Bridge(tmp_path, db, "migration-agent", "test")
    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        presence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_presence)")
        }
        message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)")
        }
        route_receipt_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(message_route_receipts)"
            )
        }
        route_profile_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(route_profiles)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == "17"
    assert "workspace_root_key" in task_columns
    assert "response_model_id" in route_profile_columns
    assert "message_dispatches" in tables
    assert "message_dispatch_retry_schedules" in tables
    assert "inference_usage" in tables
    assert "review_quorum" in task_columns
    assert {
        "client_name",
        "provider_id",
        "model_id",
        "reasoning_mode",
        "route_class",
    }.issubset(presence_columns)
    assert {
        "route_profile_id",
        "route_profile_sha256",
        "requested_provider_id",
        "requested_model_id",
        "requested_reasoning_mode",
        "requested_route_class",
        "route_request_sha256",
        "room_id",
    }.issubset(message_columns)
    assert {
        "route_profiles",
        "message_route_receipts",
        "provider_connections",
        "rooms",
        "room_memberships",
        "room_automation_policies",
        "room_discussions",
        "memories",
    }.issubset(tables)
    assert "observed_route_class" in route_receipt_columns


def test_schema_v16_usage_coverage_migrates_from_existing_reported_values(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '16');
            CREATE TABLE inference_usage (
                scope TEXT NOT NULL,
                message_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                reply_message_id TEXT NOT NULL,
                route_profile_id TEXT,
                provider_id TEXT,
                model_id TEXT,
                reasoning_mode TEXT,
                route_class TEXT,
                usage_status TEXT NOT NULL,
                usage_source TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cached_input_tokens INTEGER,
                reasoning_tokens INTEGER,
                reported_calls INTEGER NOT NULL,
                total_calls INTEGER NOT NULL,
                total_tokens_derived INTEGER NOT NULL,
                recorded_utc TEXT NOT NULL,
                inference_receipt_sha256 TEXT NOT NULL,
                usage_sha256 TEXT NOT NULL,
                PRIMARY KEY(scope, message_id, agent_id)
            );
            INSERT INTO inference_usage VALUES (
                'test', 'message-1', 'agent-1', 'reply-1', NULL,
                'provider-1', 'model-1', NULL, 'relay', 'reported', 'legacy',
                11, 7, 18, NULL, 3, 1, 1, 0,
                '2026-08-16T00:00:00Z',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            );
            """
        )

    Bridge(tmp_path, db, "migration-agent", "test")

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        row = connection.execute(
            "SELECT * FROM inference_usage WHERE message_id='message-1'"
        ).fetchone()
    assert version == "17"
    assert row["input_tokens_reported_calls"] == 1
    assert row["output_tokens_reported_calls"] == 1
    assert row["total_tokens_reported_calls"] == 1
    assert row["cached_input_tokens_reported_calls"] == 0
    assert row["reasoning_tokens_reported_calls"] == 1


def test_schema_v11_discussions_migrate_without_duplicate_rounds_to_v17(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '11');
            CREATE TABLE rooms (
                scope TEXT NOT NULL,
                room_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_by TEXT NOT NULL,
                archived INTEGER NOT NULL,
                created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL,
                room_sha256 TEXT NOT NULL,
                PRIMARY KEY(scope, room_id)
            );
            INSERT INTO rooms VALUES (
                'test', 'legacy-room', 'Legacy', 'human-operator', 0,
                '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            CREATE TABLE room_discussions (
                scope TEXT NOT NULL,
                discussion_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                starter_agent_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_round INTEGER NOT NULL,
                max_rounds INTEGER NOT NULL,
                max_messages INTEGER NOT NULL,
                stagnation_rounds INTEGER NOT NULL,
                message_count INTEGER NOT NULL,
                stagnation_count INTEGER NOT NULL,
                last_round_digest TEXT,
                stop_reason TEXT,
                created_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL,
                discussion_sha256 TEXT NOT NULL,
                PRIMARY KEY(scope, discussion_id)
            );
            INSERT INTO room_discussions VALUES (
                'test', 'legacy-active', 'legacy-room', 'legacy-task',
                'Active round', 'human-operator', 'active', 3, 4, 40, 2,
                10, 0, NULL, NULL, '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            );
            INSERT INTO room_discussions VALUES (
                'test', 'legacy-completed', 'legacy-room', 'legacy-task',
                'Completed round', 'human-operator', 'completed', 2, 4, 40, 2,
                8, 0, NULL, 'consensus', '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z',
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
            );
            """
        )

    Bridge(tmp_path, db, "migration-agent", "test")

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT * FROM room_discussions ORDER BY discussion_id"
        ).fetchall()
    assert version == "17"
    assert [(row["discussion_id"], row["processed_round"]) for row in rows] == [
        ("legacy-active", 2),
        ("legacy-completed", 2),
    ]
    assert all(
        row["discussion_sha256"]
        == stable_sha256(Bridge._discussion_row_payload(row))
        for row in rows
    )


def test_schema_v14_profile_route_migrates_nullable_and_fails_closed_without_sha(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    legacy_request = {
        "route_profile_id": "legacy-v14-route",
        "target_agent_id": "legacy-worker",
        "requested_provider_id": "relay-legacy",
        "requested_model_id": "model-legacy",
        "requested_reasoning_mode": "high",
        "requested_route_class": "relay",
    }
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '14');
            CREATE TABLE messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                room_id TEXT NOT NULL DEFAULT 'lobby',
                task_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                priority TEXT NOT NULL,
                reply_to TEXT,
                artifact_paths_json TEXT NOT NULL,
                route_profile_id TEXT,
                requested_provider_id TEXT,
                requested_model_id TEXT,
                requested_reasoning_mode TEXT,
                requested_route_class TEXT,
                route_request_sha256 TEXT,
                discussion_id TEXT,
                discussion_round INTEGER,
                discussion_role TEXT,
                created_utc TEXT NOT NULL,
                acknowledged_utc TEXT,
                content_sha256 TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """INSERT INTO messages(
                   message_id, scope, room_id, task_id, sender, recipient,
                   subject, body, priority, reply_to, artifact_paths_json,
                   route_profile_id, requested_provider_id, requested_model_id,
                   requested_reasoning_mode, requested_route_class,
                   route_request_sha256, discussion_id, discussion_round,
                   discussion_role, created_utc, acknowledged_utc, content_sha256
               ) VALUES (
                   'legacy-v14-message', 'test', 'lobby', 'legacy-v14-task',
                   'sender', 'legacy-worker', 'legacy route', 'legacy route',
                   'normal', NULL, '[]', 'legacy-v14-route', 'relay-legacy',
                   'model-legacy', 'high', 'relay', ?, NULL, NULL, NULL,
                   '2026-08-15T00:00:00Z', NULL, ?
               )""",
            (stable_sha256(legacy_request), "d" * 64),
        )

    bridge = Bridge(
        tmp_path,
        db,
        "legacy-worker",
        "test",
        provider_id="relay-legacy",
        model_id="model-legacy",
        reasoning_mode="high",
        route_class="relay",
    )
    bridge.upsert_route_profile(
        {
            "route_id": "legacy-v14-route",
            "agent_id": "legacy-worker",
            "provider_id": "relay-legacy",
            "model_id": "model-legacy",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        profile_sha_column = next(
            row
            for row in connection.execute("PRAGMA table_info(messages)")
            if row[1] == "route_profile_sha256"
        )
        stored_profile_sha256 = connection.execute(
            """SELECT route_profile_sha256 FROM messages
                 WHERE message_id='legacy-v14-message'"""
        ).fetchone()[0]

    assert version == "17"
    assert profile_sha_column[3] == 0
    assert stored_profile_sha256 is None
    message = bridge.poll_messages({})["messages"][0]
    assert message["route_profile_sha256"] is None
    assert message["route_request"]["route_profile_sha256"] is None
    assert message["route_evaluation"]["mismatches"] == [
        "route_request_sha256",
        "route_profile_sha256",
    ]
    with pytest.raises(
        BridgeError,
        match="route_request_sha256, route_profile_sha256",
    ):
        bridge.ack_message({"message_id": "legacy-v14-message"})


def test_monitor_time_order_indexes_are_installed(tmp_path: Path) -> None:
    bridge = make_bridge(tmp_path, "index-audit")
    expected = {
        "messages": "idx_messages_scope_created",
        "message_dispatches": "idx_message_dispatches_scope_updated",
        "inference_usage": "idx_inference_usage_scope_time",
        "message_dispatch_retry_schedules": "idx_dispatch_retry_schedule_due",
        "tasks": "idx_tasks_scope_updated",
        "work_updates": "idx_work_updates_scope_created",
        "peer_calls": "idx_peer_calls_scope_request_time",
        "peer_reviews": "idx_peer_reviews_scope_review_time",
        "integration_records": "idx_integration_records_scope_recorded",
        "events": "idx_events_scope_created",
    }
    with sqlite3.connect(bridge.db_path) as connection:
        for table, index in expected.items():
            indexes = {row[1] for row in connection.execute(f"PRAGMA index_list({table})")}
            assert index in indexes


def test_schema_v8_routes_migrate_without_granting_legacy_class_proof_to_v17(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    legacy_request = {
        "route_profile_id": "legacy-official",
        "target_agent_id": "legacy-worker",
        "requested_provider_id": "same-provider-label",
        "requested_model_id": "same-model-label",
        "requested_reasoning_mode": None,
    }
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '8');
            CREATE TABLE messages (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                room_id TEXT NOT NULL DEFAULT 'lobby',
                task_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                priority TEXT NOT NULL,
                reply_to TEXT,
                artifact_paths_json TEXT NOT NULL,
                route_profile_id TEXT,
                requested_provider_id TEXT,
                requested_model_id TEXT,
                requested_reasoning_mode TEXT,
                route_request_sha256 TEXT,
                created_utc TEXT NOT NULL,
                acknowledged_utc TEXT,
                content_sha256 TEXT NOT NULL
            );
            CREATE TABLE message_route_receipts (
                scope TEXT NOT NULL,
                message_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                observed_provider_id TEXT,
                observed_model_id TEXT,
                observed_reasoning_mode TEXT,
                route_status TEXT NOT NULL,
                acknowledged_utc TEXT NOT NULL,
                receipt_sha256 TEXT NOT NULL,
                PRIMARY KEY(scope, message_id, agent_id, session_id),
                FOREIGN KEY(message_id) REFERENCES messages(message_id)
            );
            CREATE TABLE agent_presence (
                scope TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                transport TEXT NOT NULL,
                client_name TEXT,
                provider_id TEXT,
                model_id TEXT,
                reasoning_mode TEXT,
                last_seen_utc TEXT NOT NULL,
                last_seen_epoch REAL NOT NULL,
                PRIMARY KEY(scope, agent_id, session_id)
            );
            """
        )
        connection.execute(
            """INSERT INTO messages(
                   message_id, scope, room_id, task_id, sender, recipient,
                   subject, body, priority, reply_to, artifact_paths_json,
                   route_profile_id, requested_provider_id, requested_model_id,
                   requested_reasoning_mode, route_request_sha256, created_utc,
                   acknowledged_utc, content_sha256
               ) VALUES (
                   'legacy-message', 'test', 'lobby', 'legacy-task', 'sender',
                   'legacy-worker', 'legacy route', 'legacy route', 'normal',
                   NULL, '[]', 'legacy-official', 'same-provider-label',
                   'same-model-label', NULL, ?, '2026-08-12T00:00:00Z', NULL, ?
               )""",
            (stable_sha256(legacy_request), "c" * 64),
        )

    bridge = Bridge(
        tmp_path,
        db,
        "legacy-worker",
        "test",
        provider_id="same-provider-label",
        model_id="same-model-label",
        route_class="official",
    )
    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)")
        }
        route_receipt_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(message_route_receipts)"
            )
        }
        presence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_presence)")
        }
        migrated_class = connection.execute(
            """SELECT requested_route_class FROM messages
               WHERE message_id='legacy-message'"""
        ).fetchone()[0]

    assert version == "17"
    assert "requested_route_class" in message_columns
    assert "observed_route_class" in route_receipt_columns
    assert "route_class" in presence_columns
    assert migrated_class is None
    evaluation = bridge.poll_messages({})["messages"][0]["route_evaluation"]
    assert evaluation["status"] == "mismatch"
    assert evaluation["mismatches"] == [
        "route_request_sha256",
        "route_profile_sha256",
        "route_class",
    ]
    with pytest.raises(
        BridgeError,
        match="route_request_sha256, route_profile_sha256, route_class",
    ):
        bridge.ack_message({"message_id": "legacy-message"})


def test_schema_v2_presence_migrates_additively_to_v17(tmp_path: Path) -> None:
    state = tmp_path / ".peerbridge"
    state.mkdir()
    db = state / "bridge.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '2');
            CREATE TABLE agent_presence (
                scope TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                transport TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                last_seen_epoch REAL NOT NULL,
                PRIMARY KEY(scope, agent_id, session_id)
            );
            INSERT INTO agent_presence VALUES (
                'test', 'legacy-agent', 'legacy-session', 'stdio',
                '2026-08-11T00:00:00Z', 1786406400
            );
            """
        )

    bridge = Bridge(
        tmp_path,
        db,
        "migration-agent",
        "test",
        client_name="codex",
        provider_id="openai-official",
        model_id="gpt",
        route_class="official",
    )
    bridge.touch_presence()

    with sqlite3.connect(db) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        presence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_presence)")
        }
        legacy = connection.execute(
            """SELECT client_name, provider_id, model_id, route_class
               FROM agent_presence WHERE agent_id='legacy-agent'"""
        ).fetchone()
        current = connection.execute(
            """SELECT client_name, provider_id, model_id, route_class
               FROM agent_presence WHERE agent_id='migration-agent'"""
        ).fetchone()

    assert version == "17"
    assert {
        "client_name",
        "provider_id",
        "model_id",
        "reasoning_mode",
        "route_class",
    }.issubset(presence_columns)
    assert legacy == (None, None, None, None)
    assert current == ("codex", "openai-official", "gpt", "official")
