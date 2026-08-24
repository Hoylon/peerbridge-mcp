from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import peerbridge_mcp.cli as cli_module
import pytest
from peerbridge_mcp import server as server_module
from peerbridge_mcp.agent_identity import (
    AgentIdentityError,
    ensure_agent_identity_capability,
    revoke_agent_identity_capability,
    verify_agent_identity_capability,
)
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.execution_governance import ExecutionGovernance


def _database(root: Path) -> Path:
    return root / ".peerbridge" / "peerbridge.sqlite3"


def _initialize(root: Path, scope: str = "identity-test") -> Path:
    database = _database(root)
    Bridge(root, database, "identity-provisioner", scope)
    return database


def _convert_to_legacy_v1(database: Path, capability_path: Path) -> None:
    data = json.loads(capability_path.read_text(encoding="utf-8"))
    data["schema"] = "peerbridge.agent-identity-capability.v1"
    for key in (
        "allowed_tools",
        "issued_by",
        "route_binding",
        "bound_room_id",
        "bound_room_session_id",
        "bound_route_profile_id",
        "bound_route_profile_sha256",
    ):
        data.pop(key, None)
    data["capability_sha256"] = _descriptor_sha256(data)
    capability_path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_identity_capabilities SET capability_sha256=? "
            "WHERE capability_id=?",
            (data["capability_sha256"], data["capability_id"]),
        )


def _descriptor_sha256(data: dict[str, object]) -> str:
    descriptor = {
        key: data[key]
        for key in (
            "schema",
            "capability_id",
            "workspace_root_key",
            "scope",
            "agent_id",
            "secret_file_relpath",
            "issued_utc",
            "token_sha256",
        )
    }
    if data.get("schema") == "peerbridge.agent-identity-capability.v2":
        descriptor["allowed_tools"] = data["allowed_tools"]
        descriptor["issued_by"] = data["issued_by"]
        if "route_binding" in data:
            descriptor["route_binding"] = data["route_binding"]
        if "bound_room_id" in data:
            descriptor["bound_room_id"] = data["bound_room_id"]
        for key in (
            "bound_room_session_id",
            "bound_route_profile_id",
            "bound_route_profile_sha256",
        ):
            if key in data:
                descriptor[key] = data[key]
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_serve(
    root: Path,
    agent_id: str,
    *,
    scope: str = "identity-test",
    capability: Path | None = None,
    allowed_tools: tuple[str, ...] = (),
    route_labels: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "peerbridge_mcp",
        "serve",
        "--project-root",
        str(root),
        "--agent-id",
        agent_id,
        "--scope",
        scope,
    ]
    if capability is not None:
        command.extend(("--identity-capability", str(capability)))
    for tool in allowed_tools:
        command.extend(("--allow-tool", tool))
    route_options = {
        "client_name": "--client-name",
        "provider_id": "--provider-id",
        "model_id": "--model-id",
        "reasoning_mode": "--reasoning-mode",
        "route_class": "--route-class",
    }
    for key, option in route_options.items():
        if route_labels is not None and route_labels.get(key) is not None:
            command.extend((option, route_labels[key]))
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {}},
        }
    )
    return subprocess.run(
        command,
        input=request + "\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )


def test_capability_binds_workspace_scope_and_agent(tmp_path: Path) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "agent-a"
    )

    verified = verify_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "agent-a",
        capability.path,
    )
    assert verified == capability
    with pytest.raises(AgentIdentityError, match="another Agent session"):
        verify_agent_identity_capability(
            tmp_path,
            database,
            "identity-test",
            "agent-b",
            capability.path,
        )


def test_capability_releases_database_handle_before_return(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = _initialize(workspace)

    capability = ensure_agent_identity_capability(
        workspace, database, "identity-test", "agent-a"
    )

    assert capability.path.is_file()
    shutil.rmtree(workspace)
    assert not workspace.exists()


def test_serve_rejects_missing_capability_before_presence_write(tmp_path: Path) -> None:
    database = _initialize(tmp_path)
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM agent_presence").fetchone()[0]

    completed = _run_serve(tmp_path, "agent-a")

    assert completed.returncode == 2
    assert "--identity-capability" in completed.stderr
    with sqlite3.connect(database) as connection:
        after = connection.execute("SELECT COUNT(*) FROM agent_presence").fetchone()[0]
    assert after == before


def test_serve_rejects_capability_for_another_agent_without_state_mutation(
    tmp_path: Path,
) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "agent-a"
    )
    with sqlite3.connect(database) as connection:
        before_presence = connection.execute(
            "SELECT COUNT(*) FROM agent_presence"
        ).fetchone()[0]
        before_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    completed = _run_serve(tmp_path, "agent-b", capability=capability.path)

    assert completed.returncode == 2
    assert "another Agent session" in completed.stderr
    with sqlite3.connect(database) as connection:
        after_presence = connection.execute(
            "SELECT COUNT(*) FROM agent_presence"
        ).fetchone()[0]
        after_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert (after_presence, after_events) == (before_presence, before_events)


def test_serve_rejects_tampered_capability_secret(tmp_path: Path) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "agent-a"
    )
    data = json.loads(capability.path.read_text(encoding="utf-8"))
    data["secret_token"] = "x" * len(data["secret_token"])
    capability.path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    completed = _run_serve(tmp_path, "agent-a", capability=capability.path)

    assert completed.returncode == 2
    assert "secret does not match" in completed.stderr


def test_known_secret_cannot_rebind_capability_to_another_agent(
    tmp_path: Path,
) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "agent-a"
    )
    data = json.loads(capability.path.read_text(encoding="utf-8"))
    data["agent_id"] = "agent-b"
    data["capability_sha256"] = _descriptor_sha256(data)
    capability.path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    completed = _run_serve(tmp_path, "agent-b", capability=capability.path)

    assert completed.returncode == 2
    assert "registry binding does not match" in completed.stderr


def test_capability_copy_cannot_cross_workspace_boundary(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source_database = _initialize(source_root)
    target_database = _initialize(target_root)
    capability = ensure_agent_identity_capability(
        source_root, source_database, "identity-test", "agent-a"
    )
    target_capability_root = target_database.parent / "identity-capabilities"
    target_capability_root.mkdir()
    copied = target_capability_root / capability.path.name
    shutil.copy2(capability.path, copied)

    with pytest.raises(AgentIdentityError, match="another Agent session"):
        verify_agent_identity_capability(
            target_root,
            target_database,
            "identity-test",
            "agent-a",
            copied,
        )


def test_legitimate_capability_allows_serve_and_revocation_blocks_reuse(
    tmp_path: Path,
) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "agent-a",
        allowed_tools=("bridge_status",),
    )

    completed = _run_serve(tmp_path, "agent-a", capability=capability.path)

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout.strip())
    status = json.loads(response["result"]["content"][0]["text"])
    assert status["agent_id"] == "agent-a"
    assert revoke_agent_identity_capability(
        database, "identity-test", capability.capability_id
    )
    blocked = _run_serve(tmp_path, "agent-a", capability=capability.path)
    assert blocked.returncode == 2
    assert "unknown or revoked" in blocked.stderr


def test_v2_empty_tool_allowlist_denies_all_without_fallback(tmp_path: Path) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "deny-all-agent", allowed_tools=()
    )
    data = json.loads(capability.path.read_text(encoding="utf-8"))
    for key in (
        "route_binding",
        "bound_room_id",
        "bound_room_session_id",
        "bound_route_profile_id",
        "bound_route_profile_sha256",
    ):
        data.pop(key)
    data["capability_sha256"] = _descriptor_sha256(data)
    capability.path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_identity_capabilities SET capability_sha256=? "
            "WHERE capability_id=?",
            (data["capability_sha256"], capability.capability_id),
        )

    verified = verify_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "deny-all-agent",
        capability.path,
    )
    assert verified.allowed_tools == ()
    assert verified.route_binding is None
    assert verified.bound_room_id is None

    completed = _run_serve(
        tmp_path,
        "deny-all-agent",
        capability=capability.path,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["error"]["message"] == "Tool is not allowed: bridge_status"


def test_v2_capability_binds_room_session_and_route_revision(tmp_path: Path) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "room-agent",
        allowed_tools=("bridge_status",),
        bound_room_id="alpha",
        bound_room_session_id="room-session-one",
        bound_route_profile_id="route-one",
        bound_route_profile_sha256="a" * 64,
    )

    verified = verify_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "room-agent",
        capability.path,
    )

    assert verified.bound_room_id == "alpha"
    assert verified.bound_room_session_id == "room-session-one"
    assert verified.bound_route_profile_id == "route-one"
    assert verified.bound_route_profile_sha256 == "a" * 64


def test_only_v1_capability_receives_safe_tool_fallback(tmp_path: Path) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "legacy-agent", allowed_tools=()
    )
    _convert_to_legacy_v1(database, capability.path)

    completed = _run_serve(
        tmp_path,
        "legacy-agent",
        capability=capability.path,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert "result" in response


def test_capability_exact_route_binding_must_match_serve_labels(
    tmp_path: Path,
) -> None:
    database = _initialize(tmp_path)
    exact_route = {
        "client_name": "openai-compatible-runner",
        "provider_id": "relay-main",
        "model_id": "deepseek",
        "reasoning_mode": "high",
        "route_class": "relay",
    }
    capability = ensure_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "route-agent",
        allowed_tools=("bridge_status",),
        route_binding=exact_route,
    )

    allowed = _run_serve(
        tmp_path,
        "route-agent",
        capability=capability.path,
        route_labels=exact_route,
    )
    mismatched = _run_serve(
        tmp_path,
        "route-agent",
        capability=capability.path,
        route_labels={**exact_route, "model_id": "another-model"},
    )

    assert allowed.returncode == 0, allowed.stderr
    assert "result" in json.loads(allowed.stdout)
    assert mismatched.returncode == 2
    assert "bound to another exact route" in mismatched.stderr


def test_active_stdio_session_is_permanently_fenced_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path,
        database,
        "identity-test",
        "active-agent",
        allowed_tools=("bridge_status",),
    )
    bridge = Bridge(tmp_path, database, "active-agent", "identity-test")
    requests = "".join(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "tools/call",
                "params": {"name": "bridge_status", "arguments": {}},
            }
        )
        + "\n"
        for index in range(3)
    )
    stdin = io.StringIO(requests)
    stdout = io.StringIO()
    real_verify = server_module.verify_agent_identity_capability
    verification_calls = 0

    def verify_then_revoke(*args: object, **kwargs: object):
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 3:
            assert revoke_agent_identity_capability(
                database, "identity-test", capability.capability_id
            )
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(
        server_module, "verify_agent_identity_capability", verify_then_revoke
    )
    monkeypatch.setattr(server_module.sys, "stdin", stdin)
    monkeypatch.setattr(server_module.sys, "stdout", stdout)

    assert server_module.serve(
        bridge,
        {"bridge_status"},
        identity_capability=capability,
    ) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert "result" in responses[0]
    assert [row["error"]["code"] for row in responses[1:]] == [-32003, -32003]
    assert all("session fenced" in row["error"]["message"] for row in responses[1:])
    assert verification_calls == 3


def test_capability_backed_serve_rejects_mutating_tool_escalation(
    tmp_path: Path,
) -> None:
    database = _initialize(tmp_path)
    capability = ensure_agent_identity_capability(
        tmp_path, database, "identity-test", "agent-a"
    )

    blocked = _run_serve(
        tmp_path,
        "agent-a",
        capability=capability.path,
        allowed_tools=("send_message",),
    )

    assert blocked.returncode == 2
    assert "outside its bound allowlist" in blocked.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0] == 0


def test_identity_cli_issues_only_fixed_non_reserved_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = _initialize(tmp_path)
    authorization = ExecutionGovernance(
        Bridge(tmp_path, database, "human-operator", "identity-test")
    ).decide_permission(
        task_id="identity-issue:agent-cli",
        agent_id="agent-cli",
        action="identity.capability.issue",
        resource_key="identity-profile:observer",
        decision="allow",
        reason="Test operator authorized one capability issue.",
        expires_epoch=time.time() + 600,
    )

    assert cli_module.main(
        [
            "identity",
            "--project-root",
            str(tmp_path),
            "--db",
            str(database),
            "--scope",
            "identity-test",
            "issue",
            "--agent-id",
            "agent-cli",
            "--profile",
            "observer",
            "--permission-decision-id",
            authorization["decision_id"],
        ]
    ) == 0
    captured = capsys.readouterr()
    issued = json.loads(captured.out)
    capability_file = json.loads(
        Path(issued["identity_capability"]).read_text(encoding="utf-8")
    )
    assert issued["profile"] == "observer"
    assert "send_message" not in issued["allowed_tools"]
    assert capability_file["secret_token"] not in captured.out

    self_attested = _run_serve(
        tmp_path,
        "agent-cli",
        capability=Path(issued["identity_capability"]),
        route_labels={"provider_id": "self-attested-provider"},
    )
    assert self_attested.returncode == 2
    assert "cannot self-attest route labels" in self_attested.stderr

    assert cli_module.main(
        [
            "identity",
            "--project-root",
            str(tmp_path),
            "--db",
            str(database),
            "--scope",
            "identity-test",
            "issue",
            "--agent-id",
            "human-operator",
            "--permission-decision-id",
            "unused-reserved-authorization",
        ]
    ) == 2
    assert "reserved operator identities" in capsys.readouterr().err

    assert cli_module.main(
        [
            "identity",
            "--project-root",
            str(tmp_path),
            "--db",
            str(database),
            "--scope",
            "identity-test",
            "issue",
            "--agent-id",
            "peerbridge-orchestrator",
            "--permission-decision-id",
            "unused-orchestrator-authorization",
        ]
    ) == 2
    assert "reserved operator identities" in capsys.readouterr().err

    assert cli_module.main(
        [
            "identity",
            "--project-root",
            str(tmp_path),
            "--db",
            str(database),
            "--scope",
            "identity-test",
            "revoke",
            "--capability-id",
            issued["capability_id"],
        ]
    ) == 2
    assert "authenticated Control Room" in capsys.readouterr().err
