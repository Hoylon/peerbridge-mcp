from __future__ import annotations

import http.client
import json
import threading

from peerbridge_mcp.approval_broker import ApprovalBroker
from peerbridge_mcp.official_agent_runtime import _AcpApprovalCallbackServer


def test_acp_loopback_callback_pauses_and_returns_session_grant() -> None:
    broker = ApprovalBroker(
        session_id="acp-session-one",
        adapter_id="grok-native-acp",
        mode="approval-required",
    )
    server = _AcpApprovalCallbackServer(broker, "test-token")
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    response_payload: list[dict[str, object]] = []

    def request_permission() -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1", int(server.server_address[1]), timeout=10
        )
        payload = json.dumps(
            {
                "toolCallId": "tool-one",
                "toolName": "Bash",
                "title": "Run tests",
                "inferredKind": "execute",
                "input": {"command": "pytest -q"},
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/approval",
            body=payload,
            headers={
                "Authorization": " ".join(("Bearer", "test-token")),
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        response_payload.append(json.loads(response.read()))
        connection.close()

    worker = threading.Thread(target=request_permission)
    worker.start()
    for _ in range(100):
        pending = broker.snapshot()["pending"]
        if pending:
            break
        worker.join(timeout=0.01)
    else:
        raise AssertionError("ACP approval callback was not published")
    broker.resolve(str(pending[0]["approval_id"]), "allow-session")
    worker.join(timeout=5)
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)

    assert not worker.is_alive()
    assert response_payload == [{"outcome": "allow_always"}]


def test_acp_loopback_callback_rejects_wrong_token_without_prompt() -> None:
    broker = ApprovalBroker(
        session_id="acp-session-two",
        adapter_id="kimi-native-acp",
        mode="approval-required",
    )
    server = _AcpApprovalCallbackServer(broker, "right-token")
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(server.server_address[1]), timeout=5
    )
    payload = b'{"toolName":"Write","inferredKind":"edit"}'
    connection.request(
        "POST",
        "/approval",
        body=payload,
        headers={
            "Authorization": " ".join(("Bearer", "wrong-token")),
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        },
    )
    response = connection.getresponse()
    assert response.status == 403
    response.read()
    connection.close()
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)

    assert broker.snapshot()["pending_count"] == 0
