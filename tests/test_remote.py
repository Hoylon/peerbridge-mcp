from __future__ import annotations

import http.client
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.credentials import credential_target
from peerbridge_mcp.remote import (
    CSRF_HEADER,
    IDENTITY_HEADER,
    PROXY_AUTH_HEADER,
    RemoteControlError,
    RemoteControlServer,
    identity_agent_id,
    make_server,
    tailscale_self_login,
)
from peerbridge_mcp.monitor import McpHumanClient


LOGIN = "operator@example.test"
CSRF = "test-csrf-token"
ACCESS = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PUBLIC_ORIGIN = "https://peerbridge.tail123.ts.net"


def test_windows_launcher_handles_empty_external_output_safely() -> None:
    launcher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "launch_remote_control.ps1"
    ).read_text(encoding="utf-8")
    assert 'if ($null -eq $Output) { $Output = "" }' in launcher
    assert 'if ($null -eq $ErrorOutput) { $ErrorOutput = "" }' in launcher
    assert "$External.WaitForExit()" in launcher
    assert "$External.Refresh()" in launcher
    assert "termination was not confirmed" in launcher
    assert "$FilePath exited with code $ExitCode" in launcher
    assert "return $Output.Trim()" in launcher
    assert "$Tail.CertDomains | Where-Object" in launcher
    assert "Tailscale HTTPS certificates are not enabled" in launcher
    assert '$Tail.BackendState -ne "Running"' in launcher
    assert "$HealthProcessId = [int]$Response.process_id" in launcher
    assert "$ObservedListenerPid -eq $ListenerPid" in launcher
    assert '"--evidence-run-id", $RequestedEvidenceRunId' in launcher
    assert "$ExistingEvidenceRunId -eq $RequestedEvidenceRunId" in launcher
    assert "function Stop-OwnedBackendTree" in launcher
    assert "launcher_start_time_utc_ticks" in launcher
    assert 'Assert-ServeConfiguration -ExpectedTarget $TargetBackend' in launcher
    assert 'Invoke-Tailscale -ArgumentList @("serve", "status", "--json")' in launcher
    assert "Tailscale Serve status is empty after configuration" in launcher
    assert "Write-ServeState" in launcher
    assert launcher.index("Assert-ServeConfiguration -ExpectedTarget $TargetBackend") < launcher.rindex("Write-ServeState")


@contextmanager
def running_server(
    root: Path,
    db: Path,
    scope: str,
    *,
    port: int = 0,
    evidence_run_id: str | None = None,
    evidence_minimum_gap_seconds: int = 0,
) -> Iterator[int]:
    server = make_server(
        root,
        db,
        scope,
        "127.0.0.1",
        port,
        {LOGIN},
        proxy_credential=ACCESS,
        csrf_token=CSRF,
        public_origin=PUBLIC_ORIGIN,
        instance_id="test-instance",
        evidence_run_id=evidence_run_id,
        evidence_minimum_gap_seconds=evidence_minimum_gap_seconds,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    merged = dict(headers or {})
    if body is not None:
        merged.setdefault("Content-Type", "application/json")
        merged.setdefault("Content-Length", str(len(body)))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def seed(root: Path, db: Path, scope: str) -> Bridge:
    bridge = Bridge(root, db, "human-operator", scope)
    bridge.send_message(
        {
            "recipient": "peer-agent",
            "task_id": "seed-task",
            "subject": "Visible seed",
            "body": "scope-local evidence",
            "priority": "normal",
        }
    )
    bridge.upsert_route_profile(
        {
            "route_id": "peer-route",
            "agent_id": "peer-agent",
            "provider_id": "relay-example",
            "model_id": "model-example",
            "reasoning_mode": "high",
            "route_class": "relay",
        }
    )
    bridge.upsert_provider_connection(
        {
            "connection_id": "provider-example",
            "display_name": "Example provider",
            "route_class": "relay",
            "provider_id": "relay-example",
            "secret_backend": "windows-credential-manager",
            "credential_target": credential_target(scope, "provider-example"),
            "endpoint_sha256": "1" * 64,
            "credential_fingerprint_sha256": "2" * 64,
            "descriptor_schema": "peerbridge.provider-credential.v2",
            "credential_version_sha256": "2" * 64,
        }
    )
    return bridge


def auth_headers(**extra: str) -> dict[str, str]:
    return {
        IDENTITY_HEADER: LOGIN,
        PROXY_AUTH_HEADER: ACCESS,
        "Origin": PUBLIC_ORIGIN,
        **extra,
    }


def message_payload(body: str = "Please review this task.") -> dict[str, object]:
    return {
        "recipient": "peer-agent",
        "task_id": "remote-task",
        "subject": "Human intervention",
        "body": body,
        "priority": "high",
    }


def test_remote_is_loopback_only_and_requires_tailnet_identity(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    with pytest.raises(RemoteControlError, match="loopback"):
        make_server(
            tmp_path,
            db,
            "scope-a",
            "0.0.0.0",
            0,
            {LOGIN},
            proxy_credential=ACCESS,
            public_origin=PUBLIC_ORIGIN,
        )

    with running_server(tmp_path, db, "scope-a") as port:
        status, _, body = request(port, "GET", "/healthz")
        assert status == 200
        assert json.loads(body) == {
            "status": "ok",
            "transport": "loopback",
            "instance_id": "test-instance",
            "process_id": os.getpid(),
            "proxy_credential_sha256": hashlib.sha256(ACCESS.encode()).hexdigest(),
        }

        status, _, body = request(port, "GET", "/")
        assert status == 200
        assert ACCESS.encode() not in body

        status, _, _ = request(port, "GET", "/api/snapshot")
        assert status == 401
        status, _, _ = request(
            port,
            "GET",
            "/api/snapshot",
            headers={IDENTITY_HEADER: "other@example.test"},
        )
        assert status == 401
        status, _, _ = request(
            port,
            "GET",
            "/api/snapshot",
            headers={
                IDENTITY_HEADER: LOGIN,
                PROXY_AUTH_HEADER: "f" * 64,
            },
        )
        assert status == 401
        status, _, _ = request(
            port,
            "GET",
            "/api/snapshot",
            headers={
                IDENTITY_HEADER: "other@example.test",
                PROXY_AUTH_HEADER: ACCESS,
            },
        )
        assert status == 403


def test_remote_page_has_desktop_mobile_navigation_and_real_controls(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    with running_server(tmp_path, db, "scope-a") as port:
        status, headers, body = request(port, "GET", "/")
    page = body.decode("utf-8")
    assert status == 200
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert "class=\"mode-tabs\"" in page
    assert "id=\"rail\"" in page
    assert "id=\"composer\"" in page
    assert "data-tab=\"activity\"" in page
    assert "id=\"discussionStop\"" in page
    assert "Tailcat · CLI" in page
    assert "@media(max-width:760px)" in page
    assert "__CSRF__" not in page
    assert ACCESS not in page


def test_health_binds_evidence_run_identity_when_enabled(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    with running_server(
        tmp_path,
        db,
        "scope-a",
        evidence_run_id="mobile-e2e-v1",
    ) as port:
        status, _, body = request(port, "GET", "/healthz")
        assert status == 200
        assert json.loads(body)["evidence_run_id"] == "mobile-e2e-v1"


def test_remote_rejects_invalid_ports_and_active_duplicate_listener(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    with pytest.raises(RemoteControlError, match="256-bit"):
        make_server(
            tmp_path,
            db,
            "scope-a",
            "127.0.0.1",
            0,
            {LOGIN},
            proxy_credential="too-short",
            public_origin=PUBLIC_ORIGIN,
        )
    for port in (-1, 65536):
        with pytest.raises(RemoteControlError, match="port"):
            make_server(
                tmp_path,
                db,
                "scope-a",
                "127.0.0.1",
                port,
                {LOGIN},
                proxy_credential=ACCESS,
                public_origin=PUBLIC_ORIGIN,
            )

    ephemeral = make_server(
        tmp_path,
        db,
        "scope-a",
        "127.0.0.1",
        0,
        {LOGIN},
        proxy_credential=ACCESS,
        public_origin=PUBLIC_ORIGIN,
    )
    try:
        assert ephemeral.server_address[1] > 0
    finally:
        ephemeral.server_close()

    with running_server(tmp_path, db, "scope-a") as port:
        with pytest.raises(OSError):
            duplicate = make_server(
                tmp_path,
                db,
                "scope-a",
                "127.0.0.1",
                port,
                {LOGIN},
                proxy_credential=ACCESS,
                public_origin=PUBLIC_ORIGIN,
            )
            duplicate.server_close()


def test_remote_main_reports_tailscale_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout() -> str:
        raise subprocess.TimeoutExpired(["tailscale", "status", "--json"], 15)

    monkeypatch.setattr("peerbridge_mcp.remote.tailscale_self_login", timeout)
    monkeypatch.setattr(
        "sys.argv",
        [
            "peerbridge-remote",
            "--public-origin",
            PUBLIC_ORIGIN,
            "--port",
            "8765",
        ],
    )
    from peerbridge_mcp.remote import main

    assert main() == 2


def test_cli_remote_forwards_mobile_evidence_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run_remote(*args: object) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr("peerbridge_mcp.remote.run_remote", fake_run_remote)
    from peerbridge_mcp.cli import main as cli_main

    assert (
        cli_main(
            [
                "remote",
                "--project-root",
                str(tmp_path),
                "--public-origin",
                PUBLIC_ORIGIN,
                "--evidence-run-id",
                "mobile-e2e-test",
                "--evidence-minimum-gap-seconds",
                "17",
            ]
        )
        == 0
    )
    forwarded = captured["args"]
    assert isinstance(forwarded, tuple)
    assert forwarded[8] == "mobile-e2e-test"
    assert forwarded[9] == 17


def test_snapshot_is_scope_bound_and_redacts_secret_metadata(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    other = Bridge(tmp_path, db, "other-agent", "scope-b")
    other.send_message(
        {
            "recipient": "other-peer",
            "task_id": "other-task",
            "subject": "Other scope",
            "body": "cross-scope-marker-must-not-appear",
            "priority": "normal",
        }
    )
    for index in range(510):
        other.send_message(
            {
                "recipient": "other-peer",
                "task_id": f"noise-{index}",
                "subject": "Other scope noise",
                "body": f"other-scope-noise-{index}",
                "priority": "low",
            }
        )

    with running_server(tmp_path, db, "scope-a") as port:
        status, headers, body = request(
            port, "GET", "/api/snapshot", headers=auth_headers()
        )
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "python" not in headers.get("Server", "").lower()
    payload = json.loads(body)
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["scope"] == "scope-a"
    assert payload["counts"] == {
        "agents": 0,
        "messages": 1,
        "tasks": 0,
        "routes": 1,
        "providers": 1,
        "dispatches": 0,
    }
    assert payload["room_id"] == "lobby"
    assert payload["rooms"][0]["room_id"] == "lobby"
    assert payload["dispatches"] == []
    assert payload["automation"]["active_discussion"] is None
    assert payload["messages"][0]["body"] == "scope-local evidence"
    assert "cross-scope-marker-must-not-appear" not in serialized
    assert LOGIN not in serialized
    assert "credential_target" not in serialized
    assert "endpoint_sha256" not in serialized
    assert len(payload["snapshot_signature"]) == 64


def test_snapshot_selects_one_safe_room(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    bridge = seed(tmp_path, db, "scope-a")
    bridge.create_room({"room_id": "remote-room", "name": "Remote Room"})
    bridge.join_room(
        {
            "room_id": "remote-room",
            "agent_id": "peer-agent",
            "route_profile_id": "peer-route",
        }
    )
    bridge.post_room_message(
        {
            "room_id": "remote-room",
            "task_id": "room-task",
            "subject": "Room-specific message",
            "body": "Only this room should be rendered.",
        }
    )
    with running_server(tmp_path, db, "scope-a") as port:
        status, _, body = request(
            port,
            "GET",
            "/api/snapshot?room_id=remote-room",
            headers=auth_headers(),
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["room_id"] == "remote-room"
        assert [row["subject"] for row in payload["messages"]] == [
            "Room-specific message"
        ]

        status, _, body = request(
            port,
            "GET",
            "/api/snapshot?room_id=bad%2Froom",
            headers=auth_headers(),
        )
        assert status == 400
        assert json.loads(body)["error"] == "invalid room ID"


def test_remote_discussion_control_uses_bounded_mcp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    calls: list[dict[str, object]] = []

    def control(_self: McpHumanClient, **kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {
            "status": str(kwargs["action"]),
            "discussion_sha256": "a" * 64,
        }

    monkeypatch.setattr(McpHumanClient, "control_discussion", control)
    with running_server(tmp_path, db, "scope-a") as port:
        status, _, body = request(
            port,
            "POST",
            "/api/discussion/control",
            headers=auth_headers(**{CSRF_HEADER: CSRF}),
            payload={
                "discussion_id": "discussion-one",
                "action": "stop",
                "extra_rounds": 2,
            },
        )
    assert status == 200
    assert json.loads(body) == {
        "discussion_id": "discussion-one",
        "discussion_sha256": "a" * 64,
        "status": "stop",
    }
    assert calls == [
        {
            "discussion_id": "discussion-one",
            "action": "stop",
            "extra_rounds": 2,
        }
    ]


def test_remote_message_uses_mcp_path_and_keeps_audit_valid(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    bridge = seed(tmp_path, db, "scope-a")
    with running_server(tmp_path, db, "scope-a") as port:
        status, _, _ = request(
            port,
            "POST",
            "/api/message",
            headers=auth_headers(**{CSRF_HEADER: "wrong"}),
            payload=message_payload(),
        )
        assert status == 403

        status, _, body = request(
            port,
            "POST",
            "/api/message",
            headers=auth_headers(**{CSRF_HEADER: CSRF}),
            payload=message_payload(),
        )
        assert status == 201
        receipt = json.loads(body)
        assert receipt["status"] == "sent"
        assert len(receipt["content_sha256"]) == 64
        assert len(receipt["operator_identity_sha256"]) == 64

    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM messages WHERE message_id=?", (receipt["message_id"],)
        ).fetchone()
        presence = connection.execute(
            "SELECT COUNT(*) FROM agent_presence WHERE agent_id=?",
            (identity_agent_id(LOGIN),),
        ).fetchone()[0]
    assert row is not None
    assert row["sender"] == identity_agent_id(LOGIN)
    assert row["body"] == "Please review this task."
    assert presence == 0
    assert bridge.verify_audit_chain()["valid"] is True


def test_remote_rejects_cross_origin_unknown_fields_and_credentials(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    headers = auth_headers(**{CSRF_HEADER: CSRF})
    with running_server(tmp_path, db, "scope-a") as port:
        status, _, _ = request(
            port,
            "POST",
            "/api/message",
            headers={**headers, "Origin": "https://attacker.invalid"},
            payload=message_payload(),
        )
        assert status == 403

        status, _, _ = request(
            port,
            "POST",
            "/api/message",
            headers={
                **headers,
                "Origin": f"http://127.0.0.1:{port}",
            },
            payload=message_payload(),
        )
        assert status == 403

        status, _, _ = request(
            port,
            "POST",
            "/api/message",
            headers={key: value for key, value in headers.items() if key != "Origin"},
            payload=message_payload(),
        )
        assert status == 403

        status, _, _ = request(
            port,
            "POST",
            "/api/message",
            headers=headers,
            payload={**message_payload(), "unexpected": True},
        )
        assert status == 400

        status, _, body = request(
            port,
            "POST",
            "/api/message",
            headers=headers,
            payload=message_payload("s" + "k-" + "a" * 32),
        )
        assert status == 400
        assert b"s" + b"k-" not in body


def test_remote_restarts_on_same_port_without_losing_messages(tmp_path: Path) -> None:
    assert RemoteControlServer.allow_reuse_address is (os.name != "nt")
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    with running_server(tmp_path, db, "scope-a") as first_port:
        status, _, _ = request(
            first_port,
            "POST",
            "/api/message",
            headers=auth_headers(**{CSRF_HEADER: CSRF}),
            payload=message_payload("persist across restart"),
        )
        assert status == 201

    with running_server(tmp_path, db, "scope-a", port=first_port) as second_port:
        status, _, body = request(
            second_port, "GET", "/api/snapshot", headers=auth_headers()
        )
        assert status == 200
        assert any(
            row["body"] == "persist across restart"
            for row in json.loads(body)["messages"]
        )


def test_tailscale_identity_discovery_does_not_require_persisted_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "Self": {"UserID": 42},
                "User": {"42": {"LoginName": LOGIN}},
            }
        )

    executable = tmp_path / "tailscale.exe"
    executable.write_bytes(b"stub")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return Completed()

    monkeypatch.setattr("peerbridge_mcp.remote._tailscale_executable", lambda: executable)
    monkeypatch.setattr("peerbridge_mcp.remote.subprocess.run", fake_run)
    assert tailscale_self_login() == LOGIN
    assert observed["command"] == [str(executable), "status", "--json"]
    assert observed["cwd"] == executable.parent
    assert isinstance(observed["env"], dict)


def test_real_phone_evidence_flow_is_hashed_sealed_and_audited(
    tmp_path: Path,
) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    run_id = "physical-phone-001"
    headers = auth_headers(
        **{CSRF_HEADER: CSRF, "User-Agent": "Mobile Safari Physical Phone"}
    )
    device_nonce = "device-" + "a" * 32
    first_session = "session-" + "b" * 32
    second_session = "session-" + "c" * 32
    viewport = {"width": 390, "height": 844, "max_touch_points": 5}
    with running_server(
        tmp_path,
        db,
        "scope-a",
        evidence_run_id=run_id,
    ) as port:
        status, _, body = request(
            port, "GET", "/api/e2e/status", headers=auth_headers()
        )
        assert status == 200
        evidence_status = json.loads(body)
        assert evidence_status["expected_task_id"] == f"remote-e2e-{run_id}"

        status, _, body = request(
            port, "GET", "/api/snapshot", headers=auth_headers()
        )
        snapshot = json.loads(body)
        assert status == 200
        assert snapshot["instance_id"] == "test-instance"
        status, _, body = request(
            port,
            "POST",
            "/api/e2e/session",
            headers=headers,
            payload={
                "phase": "initial",
                "device_nonce": device_nonce,
                "session_nonce": first_session,
                "viewport": viewport,
                "snapshot_signature": snapshot["snapshot_signature"],
                "disconnect_challenge": None,
            },
        )
        assert status == 200
        challenge = json.loads(body)["disconnect_challenge"]

        status, _, body = request(
            port,
            "POST",
            "/api/message",
            headers=headers,
            payload={
                **message_payload("physical phone evidence message"),
                "task_id": f"remote-e2e-{run_id}",
            },
        )
        message = json.loads(body)
        assert status == 201
        assert message["evidence_recorded"] is True

        status, _, _ = request(
            port,
            "POST",
            "/api/e2e/disconnect",
            headers=headers,
            payload={
                "device_nonce": device_nonce,
                "disconnect_challenge": challenge,
            },
        )
        assert status == 200

        status, _, body = request(
            port, "GET", "/api/snapshot", headers=auth_headers()
        )
        reconnect_snapshot = json.loads(body)
        assert status == 200
        status, _, body = request(
            port,
            "POST",
            "/api/e2e/session",
            headers=headers,
            payload={
                "phase": "reconnect",
                "device_nonce": device_nonce,
                "session_nonce": second_session,
                "viewport": viewport,
                "snapshot_signature": reconnect_snapshot["snapshot_signature"],
                "disconnect_challenge": challenge,
            },
        )
        assert status == 200
        assert json.loads(body)["complete"] is True

    evidence = tmp_path / ".peerbridge" / "evidence" / run_id
    trace = json.loads((evidence / "browser-trace.json").read_text(encoding="utf-8"))
    audit = json.loads((evidence / "audit-verification.json").read_text(encoding="utf-8"))
    serialized = json.dumps(trace, sort_keys=True)
    assert trace["schema"] == "peerbridge.mobile-browser-reconnect-trace.v2"
    assert trace["network_layer_node_identity_attested"] is False
    assert trace["sessions"][0]["browser_session_id_sha256"] != trace["sessions"][1]["browser_session_id_sha256"]
    assert trace["sessions"][0]["browser_device_continuity_sha256"] == trace["sessions"][1]["browser_device_continuity_sha256"]
    assert trace["sessions"][1]["observed_message_id"] == message["message_id"]
    assert audit["valid"] is True
    assert audit["message_id"] == message["message_id"]
    assert LOGIN not in serialized
    assert device_nonce not in serialized
    assert first_session not in serialized
    assert second_session not in serialized


def test_evidence_run_id_rejects_windows_path_punctuation(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    seed(tmp_path, db, "scope-a")
    with pytest.raises(ValueError, match="run ID"):
        make_server(
            tmp_path,
            db,
            "scope-a",
            "127.0.0.1",
            0,
            {LOGIN},
            proxy_credential=ACCESS,
            public_origin=PUBLIC_ORIGIN,
            evidence_run_id="invalid:windows-run",
        )
