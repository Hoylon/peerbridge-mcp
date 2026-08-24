from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from peerbridge_mcp.acpx_runner import AcpxRunner, SUPPORTED_AGENTS
from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.openai_compatible_runner import CLIENT_NAME, RunnerConfig, _runtime_model_id
from peerbridge_mcp.secret_scan import redact_secrets


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


def _safe(value: str, label: str) -> str:
    text = value.strip()
    if not SAFE_NAME.fullmatch(text):
        raise ValueError(f"{label} is not a safe identifier")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one create-only provider-bound Agent-to-PeerBridge MCP E2E gate."
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--db", default=".peerbridge/peerbridge.sqlite3")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument(
        "--route-class", choices=("official", "relay"), default="official"
    )
    parser.add_argument("--client", choices=sorted(SUPPORTED_AGENTS), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-mode")
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--receipt-name", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.project_root).resolve()
        client = _safe(args.client, "client")
        session_id = _safe(args.session_id, "session ID")
        receipt_name = _safe(args.receipt_name, "receipt name")
        scope = _safe(args.scope, "scope")
        agent_id = _safe(args.agent_id, "agent ID")
        db_path = (root / args.db).resolve()
        Bridge(root, db_path, agent_id, scope, session_id=session_id)
        identity_capability = ensure_agent_identity_capability(
            root,
            db_path,
            scope,
            agent_id,
            allowed_tools=("bridge_status",),
            route_binding={
                "client_name": CLIENT_NAME,
                "provider_id": _safe(args.provider_id, "provider ID"),
                "model_id": _runtime_model_id(args.model),
                "reasoning_mode": args.reasoning_mode,
                "route_class": args.route_class,
            },
            bound_room_id=_safe(args.room_id, "room ID"),
        )
        output = root / ".peerbridge" / "receipts" / f"{receipt_name}.json"
        if output.exists():
            raise FileExistsError("receipt already exists")
        result = AcpxRunner(
            RunnerConfig(
                project_root=root,
                db_path=db_path,
                scope=scope,
                connection_id=_safe(args.connection_id, "connection ID"),
                route_class=args.route_class,
                provider_id=_safe(args.provider_id, "provider ID"),
                model=args.model,
                reasoning_mode=args.reasoning_mode,
                room_id=_safe(args.room_id, "room ID"),
                session_id=session_id,
                agent_id=agent_id,
                identity_capability_path=identity_capability.path,
                timeout_seconds=args.timeout_seconds,
                max_tool_rounds=1,
                allowed_tools=("bridge_status",),
            ),
            credential_target=f"ACPX:{client}",
            client_name=client,
        ).run(
            [
                {
                    "role": "system",
                    "content": (
                        "This is an auditable capability test. Call the exposed "
                        "bridge_status tool exactly once, then return a one-sentence "
                        "acknowledgement. Do not disclose credentials or private data."
                    ),
                },
                {
                    "role": "user",
                    "content": "Call bridge_status exactly once now.",
                },
            ],
            message_id=session_id,
        )
        receipt = dict(result.receipt)
        if receipt.get("mcp_canonical_tools_called") != ["bridge_status"]:
            raise RuntimeError("Agent did not execute bridge_status")
        if receipt.get("mcp_tool_call_count") != 1:
            raise RuntimeError("Agent did not execute exactly one MCP tool call")
        if receipt.get("mcp_allowed_tool_call_count") != 1:
            raise RuntimeError("Agent MCP allowlist evidence was incomplete")
        if receipt.get("mcp_unrecognized_tool_call_count") != 0:
            raise RuntimeError("Agent attempted an unrecognized PeerBridge MCP tool")
        if receipt.get("mcp_tool_error_count") != 0:
            raise RuntimeError("Agent MCP tool invocation returned an error")
        if client == "codex" and receipt.get("session_soft_close_confirmed") is not True:
            raise RuntimeError("Codex ACPX session did not close cleanly")
        if receipt.get("credential_values_recorded") is not False:
            raise RuntimeError("credential recording contract failed")
        if receipt.get("route_class") != args.route_class:
            raise RuntimeError("provider route class drifted")

        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(
                receipt,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        summary = {
            "status": "PASS",
            "client": receipt.get("observed_agent_name"),
            "client_version": receipt.get("observed_agent_version"),
            "model": receipt.get("observed_model"),
            "route_class": receipt.get("route_class"),
            "provider_id": receipt.get("requested_provider_id"),
            "mcp_tools": receipt.get("mcp_canonical_tools_called"),
            "mcp_tool_call_count": receipt.get("mcp_tool_call_count"),
            "auxiliary_tool_call_count": receipt.get(
                "agent_auxiliary_tool_call_count"
            ),
            "credential_values_recorded": receipt.get(
                "credential_values_recorded"
            ),
            "receipt_sha256": receipt.get("receipt_sha256"),
            "receipt_path": output.relative_to(root).as_posix(),
        }
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        safe_error = redact_secrets(str(exc)).strip()
        if len(safe_error) > 300:
            safe_error = safe_error[:297] + "..."
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": safe_error,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
