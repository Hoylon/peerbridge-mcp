from __future__ import annotations

import sqlite3

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.trust_timeline import TrustTimeline, TrustTimelineError


def _timeline(tmp_path) -> TrustTimeline:
    (tmp_path / "source.txt").write_text("source v1\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("other\n", encoding="utf-8")
    bridge = Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "trust.sqlite3",
        "human-operator",
        "trust-test",
    )
    return TrustTimeline(bridge)


def _evidence_set(timeline: TrustTimeline, task_id: str = "task-one"):
    test = timeline.record(
        task_id=task_id,
        stage="test",
        statement="Focused tests passed against this source.",
        artifact_paths=["source.txt"],
        record_id=f"{task_id}:test",
    )
    proof = timeline.record(
        task_id=task_id,
        stage="proof",
        statement="Proof was captured from this source.",
        artifact_paths=["source.txt"],
        related_record_ids=[test["record_id"]],
        record_id=f"{task_id}:proof",
    )
    review = timeline.record(
        task_id=task_id,
        stage="review",
        statement="Reviewer inspected the exact source.",
        artifact_paths=["source.txt"],
        related_record_ids=[proof["record_id"]],
        record_id=f"{task_id}:review",
    )
    return test, proof, review


def _human_decision(timeline: TrustTimeline, task_id: str = "task-one"):
    return timeline.record(
        task_id=task_id,
        stage="decision",
        statement="The human operator accepted the exact reviewed source.",
        artifact_paths=["source.txt"],
        record_id=f"{task_id}:decision",
    )


def test_timeline_marks_source_stale_immediately_after_change(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    record = timeline.record(
        task_id="task-one",
        stage="test",
        statement="Test passed.",
        artifact_paths=["source.txt"],
        record_id="test-one",
    )
    live = timeline.timeline("task-one")[0]
    assert live["record_id"] == record["record_id"]
    assert live["stale"] is False
    assert live["source_bindings"][0]["live_sha256"] == record["source_bindings"][0]["sha256"]

    (tmp_path / "source.txt").write_text("source v2\n", encoding="utf-8")
    stale = timeline.timeline("task-one")[0]
    assert stale["stale"] is True
    assert stale["source_bindings"][0]["stale_reason"] == "source_changed"


def test_completion_requires_test_proof_review_on_one_fresh_source(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    test, proof, review = _evidence_set(timeline)
    decision = _human_decision(timeline)
    completion = timeline.record_completion(
        task_id="task-one",
        statement="The human accepted completion on the exact reviewed source.",
        evidence_record_ids=[
            test["record_id"],
            proof["record_id"],
            review["record_id"],
            decision["record_id"],
        ],
    )
    assert completion["stage"] == "completion"
    assert len(completion["source_bindings"]) == 1
    completed = timeline.timeline("task-one")
    assert [row["stage"] for row in completed] == [
        "test",
        "proof",
        "review",
        "decision",
        "completion",
    ]
    assert all(not row["stale"] for row in completed)

    (tmp_path / "source.txt").write_text("changed after completion\n", encoding="utf-8")
    assert all(row["stale"] for row in timeline.timeline("task-one"))


def test_completion_rejects_missing_stage_mismatch_and_stale_evidence(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    test, proof, review = _evidence_set(timeline)
    decision = _human_decision(timeline)
    with pytest.raises(TrustTimelineError, match="human decision"):
        timeline.record_completion(
            task_id="task-one",
            statement="Incomplete evidence must fail.",
            evidence_record_ids=[test["record_id"], proof["record_id"]],
        )

    mismatched = timeline.record(
        task_id="task-one",
        stage="review",
        statement="Review accidentally used another source.",
        artifact_paths=["other.txt"],
        record_id="mismatched-review",
    )
    with pytest.raises(TrustTimelineError, match="one exact source state"):
        timeline.record_completion(
            task_id="task-one",
            statement="Mismatched evidence must fail.",
            evidence_record_ids=[
                test["record_id"],
                proof["record_id"],
                mismatched["record_id"],
                decision["record_id"],
            ],
        )

    (tmp_path / "source.txt").write_text("stale\n", encoding="utf-8")
    with pytest.raises(TrustTimelineError, match="stale"):
        timeline.record_completion(
            task_id="task-one",
            statement="Stale evidence must fail.",
            evidence_record_ids=[
                test["record_id"],
                proof["record_id"],
                review["record_id"],
                decision["record_id"],
            ],
        )


def test_only_human_operator_may_record_decision(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    agent = TrustTimeline(
        type(timeline.bridge)(
            tmp_path,
            timeline.bridge.db_path,
            "reviewer-agent",
            "trust-test",
        )
    )

    with pytest.raises(TrustTimelineError, match="only human-operator"):
        agent.record(
            task_id="task-one",
            stage="decision",
            statement="An Agent cannot become human authority.",
        )


def test_disagreement_preserves_both_exact_evidence_records(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    test, _proof, review = _evidence_set(timeline)
    disagreement = timeline.record_disagreement(
        task_id="task-one",
        statement="The test and reviewer reach different conclusions; human review is required.",
        evidence_record_ids=[test["record_id"], review["record_id"]],
    )
    assert disagreement["stage"] == "disagreement"
    assert disagreement["related_record_ids"] == [
        test["record_id"],
        review["record_id"],
    ]
    assert len(disagreement["source_bindings"]) == 1


def test_recheck_appends_current_binding_without_rewriting_old_record(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    original = timeline.record(
        task_id="task-one",
        stage="proof",
        statement="Original proof.",
        artifact_paths=["source.txt"],
        record_id="original-proof",
    )
    (tmp_path / "source.txt").write_text("source v2\n", encoding="utf-8")
    recheck = timeline.recheck(
        original["record_id"],
        statement="Operator rechecked the changed artifact as a new record.",
    )
    rows = {row["record_id"]: row for row in timeline.timeline("task-one")}
    assert rows[original["record_id"]]["stale"] is True
    assert rows[recheck["record_id"]]["stale"] is False
    assert recheck["related_record_ids"] == [original["record_id"]]


def test_trust_record_tampering_fails_closed(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    timeline.record(
        task_id="task-one",
        stage="claim",
        statement="Original claim.",
        record_id="claim-one",
    )
    with sqlite3.connect(timeline.bridge.db_path) as connection:
        connection.execute(
            "UPDATE trust_records SET statement='tampered' WHERE record_id='claim-one'"
        )
    with pytest.raises(TrustTimelineError, match="SHA-256"):
        timeline.timeline("task-one")


def test_evidence_stage_requires_source_or_related_record(tmp_path) -> None:
    timeline = _timeline(tmp_path)
    with pytest.raises(TrustTimelineError, match="requires a source"):
        timeline.record(
            task_id="task-one",
            stage="test",
            statement="Unbound test claim.",
        )
