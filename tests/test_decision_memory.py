from __future__ import annotations

import sqlite3

import pytest

from peerbridge_mcp.bridge import Bridge, BridgeError, stable_sha256


def _human(tmp_path) -> Bridge:
    (tmp_path / "evidence.txt").write_text("reviewed evidence\n", encoding="utf-8")
    return Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "memory.sqlite3",
        "human-operator",
        "decision-test",
    )


def _project_record(
    bridge: Bridge,
    *,
    title: str,
    body: str,
    record_type: str,
    applicability: list[str],
    supersedes_memory_id: str | None = None,
) -> dict[str, object]:
    return bridge.record_memory(
        {
            "visibility": "project",
            "record_type": record_type,
            "authority_id": "human-operator",
            "title": title,
            "body": body,
            "artifact_paths": ["evidence.txt"],
            "applicability": applicability,
            "supersedes_memory_id": supersedes_memory_id,
        }
    )


def test_typed_project_decisions_bind_human_authority_and_source(tmp_path) -> None:
    human = _human(tmp_path)
    decision = _project_record(
        human,
        title="Writers use isolated worktrees",
        body="Every write-enabled Agent uses a separate Git worktree.",
        record_type="DECISION",
        applicability=["git", "writer"],
    )
    assert decision["record_type"] == "DECISION"
    assert decision["authority_id"] == "human-operator"
    assert decision["artifact_bindings"][0]["sha256"]
    assert decision["applicability"] == ["git", "writer"]

    agent = Bridge(
        tmp_path,
        human.db_path,
        "agent-one",
        "decision-test",
    )
    with pytest.raises(BridgeError, match="human-operator"):
        agent.record_memory(
            {
                "visibility": "project",
                "record_type": "DECISION",
                "title": "Unauthorized decision",
                "body": "This must not become project authority.",
                "artifact_paths": ["evidence.txt"],
            }
        )


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    (
        ("body", "Tampered decision body."),
        ("record_type", "FACT"),
        ("authority_id", "other-agent"),
        ("applicability_json", '["other-scope"]'),
    ),
)
def test_tampered_memory_cannot_be_read_or_used_for_task_briefing(
    tmp_path, column: str, tampered_value: str
) -> None:
    human = _human(tmp_path)
    decision = _project_record(
        human,
        title="Source-bound release decision",
        body="Only exact approved memory may brief the release task.",
        record_type="DECISION",
        applicability=["release"],
    )
    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            f"UPDATE memories SET {column}=? WHERE memory_id=?",
            (tampered_value, decision["memory_id"]),
        )

    with pytest.raises(BridgeError, match="memory SHA-256 mismatch"):
        human.read_memory({"memory_id": decision["memory_id"]})
    with pytest.raises(BridgeError, match="memory SHA-256 mismatch"):
        human.brief_task(
            {"task_id": "tampered-release", "applicability": ["release"]}
        )


def test_supersession_is_append_only_and_revocation_never_revives_old_decision(
    tmp_path,
) -> None:
    human = _human(tmp_path)
    old = _project_record(
        human,
        title="Original release rule",
        body="Use the original release process.",
        record_type="DECISION",
        applicability=["release"],
    )
    new = _project_record(
        human,
        title="Revised release rule",
        body="Use the revised source-bound release process.",
        record_type="DECISION",
        applicability=["release"],
        supersedes_memory_id=str(old["memory_id"]),
    )
    history = {
        row["memory_id"]: row
        for row in human.list_memories(
            {"visibility": "project", "include_revoked": True}
        )["memories"]
    }
    assert history[old["memory_id"]]["superseded_by_memory_id"] == new["memory_id"]
    assert history[old["memory_id"]]["status"] == "active"

    first_brief = human.brief_task(
        {"task_id": "release-task", "applicability": ["release"]}
    )
    assert [row["memory_id"] for row in first_brief["records"]] == [new["memory_id"]]

    human.revoke_memory(
        {
            "memory_id": new["memory_id"],
            "reason": "Withdraw the revised rule pending a new explicit decision.",
        }
    )
    second_brief = human.brief_task(
        {"task_id": "release-task-two", "applicability": ["release"]}
    )
    assert second_brief["records"] == []


def test_applicability_and_deprecated_records_shape_task_briefing(tmp_path) -> None:
    human = _human(tmp_path)
    release = _project_record(
        human,
        title="Release fact",
        body="Release evidence must be source-bound.",
        record_type="FACT",
        applicability=["release"],
    )
    ui = _project_record(
        human,
        title="UI preference",
        body="Keep controls visible at compact widths.",
        record_type="PREFERENCE",
        applicability=["ui"],
    )
    deprecated = _project_record(
        human,
        title="Deprecate old UI preference",
        body="The old compact-width preference is no longer binding.",
        record_type="DEPRECATED",
        applicability=["ui"],
        supersedes_memory_id=str(ui["memory_id"]),
    )
    assert deprecated["record_type"] == "DEPRECATED"

    briefing = human.brief_task(
        {"task_id": "release-only", "applicability": ["release"]}
    )
    assert [row["memory_id"] for row in briefing["records"]] == [
        release["memory_id"]
    ]
    assert briefing["memory_bindings"][0]["record_type"] == "FACT"
    assert len(briefing["briefing_sha256"]) == 64


def test_decision_conflict_is_a_finding_not_an_automatic_block(tmp_path) -> None:
    human = _human(tmp_path)
    decision = _project_record(
        human,
        title="No automatic merge",
        body="Patch application and merge remain human-controlled.",
        record_type="CONSTRAINT",
        applicability=["merge"],
    )
    briefing = human.brief_task(
        {"task_id": "merge-task", "applicability": ["merge"]}
    )
    finding = human.record_decision_conflict(
        {
            "task_id": "merge-task",
            "briefing_id": briefing["briefing_id"],
            "memory_ids": [decision["memory_id"]],
            "summary": "The proposed workflow would merge without human approval.",
            "severity": "high",
        }
    )
    assert finding["status"] == "finding"
    assert finding["enforcement"] == "review-finding-only"
    with human._connect() as connection:
        row = connection.execute(
            "SELECT * FROM decision_conflict_findings WHERE finding_id=?",
            (finding["finding_id"],),
        ).fetchone()
    payload = {
        "scope": row["scope"],
        "finding_id": row["finding_id"],
        "task_id": row["task_id"],
        "reviewer": row["reviewer"],
        "briefing_id": row["briefing_id"],
        "memory_ids": [decision["memory_id"]],
        "summary": row["summary"],
        "severity": row["severity"],
        "status": row["status"],
        "created_utc": row["created_utc"],
    }
    assert stable_sha256(payload) == row["finding_sha256"]


def test_briefing_tamper_fails_closed_before_conflict_finding(tmp_path) -> None:
    human = _human(tmp_path)
    decision = _project_record(
        human,
        title="Source-bound review",
        body="Review binds the exact source state.",
        record_type="DECISION",
        applicability=["review"],
    )
    briefing = human.brief_task(
        {"task_id": "review-task", "applicability": ["review"]}
    )
    with sqlite3.connect(human.db_path) as connection:
        connection.execute(
            "UPDATE task_briefings SET task_id='tampered' WHERE briefing_id=?",
            (briefing["briefing_id"],),
        )
    with pytest.raises(BridgeError, match="not found"):
        human.record_decision_conflict(
            {
                "task_id": "review-task",
                "briefing_id": briefing["briefing_id"],
                "memory_ids": [decision["memory_id"]],
                "summary": "Must not accept a tampered briefing.",
            }
        )
