from __future__ import annotations

import json
import sqlite3

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.event_envelope import (
    EVENT_ENVELOPE_SCHEMA,
    EventEnvelopeError,
    local_event_envelopes,
    validate_event_envelope,
)
from peerbridge_mcp.trust_timeline import TrustTimeline


def _bridge(tmp_path) -> Bridge:
    return Bridge(
        tmp_path,
        tmp_path / ".peerbridge" / "events.sqlite3",
        "human-operator",
        "event-test",
    )


def test_local_event_envelope_contains_hash_metadata_but_no_payload(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    TrustTimeline(bridge).record(
        task_id="task-one",
        stage="claim",
        statement="A private statement remains inside local authority only.",
        record_id="claim-one",
    )
    envelopes = local_event_envelopes(bridge, task_id="task-one")
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["schema"] == EVENT_ENVELOPE_SCHEMA
    assert envelope["sync_state"] == "disabled-alpha-5.2"
    assert envelope["transport"] is None
    assert envelope["encryption_required"] is True
    encoded = json.dumps(envelope)
    assert "private statement" not in encoded.lower()
    assert "payload_json" not in envelope


def test_event_projection_verifies_full_chain_before_filtering(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    timeline = TrustTimeline(bridge)
    timeline.record(
        task_id="task-one",
        stage="claim",
        statement="First task.",
        record_id="first",
    )
    timeline.record(
        task_id="task-two",
        stage="claim",
        statement="Second task.",
        record_id="second",
    )
    with sqlite3.connect(bridge.db_path) as connection:
        connection.execute(
            "UPDATE events SET payload_json='{}' WHERE task_id='task-one'"
        )
    with pytest.raises(EventEnvelopeError, match="payload SHA-256"):
        local_event_envelopes(bridge, task_id="task-two")


def test_validation_rejects_enabling_transport_or_reusing_cloud_routes(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    TrustTimeline(bridge).record(
        task_id="task",
        stage="claim",
        statement="Local only.",
        record_id="local",
    )
    envelope = local_event_envelopes(bridge)[0]
    enabled = {**envelope, "sync_state": "enabled", "transport": "https"}
    with pytest.raises(EventEnvelopeError, match="disabled"):
        validate_event_envelope(enabled)
    reused = {**envelope, "collaboration_channel": "feedback"}
    with pytest.raises(EventEnvelopeError, match="boundary"):
        validate_event_envelope(reused)
