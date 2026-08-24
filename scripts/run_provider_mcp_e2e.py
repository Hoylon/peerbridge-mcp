from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from peerbridge_mcp.agent_identity import ensure_agent_identity_capability
from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.openai_compatible_runner import (
    CLIENT_NAME,
    RunnerConfig,
    _runtime_model_id,
    run_chat_completion,
)
from peerbridge_mcp.secret_scan import redact_secrets


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


def _safe(value: str, label: str) -> str:
    text = value.strip()
    if not SAFE_NAME.fullmatch(text):
        raise ValueError(f"{label} is not a safe identifier")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one content-free provider-to-PeerBridge MCP E2E gate."
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--db", default=".peerbridge/peerbridge.sqlite3")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--route-class", choices=("official", "relay", "local"), required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--response-model")
    parser.add_argument("--reasoning-mode")
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--receipt-name", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.project_root).resolve()
        run_id = _safe(args.run_id, "run ID")
        receipt_name = _safe(args.receipt_name, "receipt name")
        scope = _safe(args.scope, "scope")
        agent_id = _safe(args.agent_id, "agent ID")
        db_path = (root / args.db).resolve()
        Bridge(root, db_path, agent_id, scope, session_id=run_id)
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
        config = RunnerConfig(
            project_root=root,
            db_path=db_path,
            scope=scope,
            connection_id=_safe(args.connection_id, "connection ID"),
            route_class=args.route_class,
            provider_id=_safe(args.provider_id, "provider ID"),
            model=args.model,
            response_model=args.response_model,
            reasoning_mode=args.reasoning_mode,
            room_id=_safe(args.room_id, "room ID"),
            session_id=run_id,
            agent_id=agent_id,
            identity_capability_path=identity_capability.path,
            timeout_seconds=args.timeout_seconds,
            max_http_attempts=3,
            retry_backoff_seconds=0.5,
            max_tool_rounds=1,
            allowed_tools=("bridge_status",),
        )
        result = run_chat_completion(
            config,
            [
                {
                    "role": "system",
                    "content": (
                        "This is an auditable capability test. Call bridge_status exactly "
                        "once before a one-sentence acknowledgement. Never reveal credentials."
                    ),
                },
                {"role": "user", "content": "Call bridge_status now."},
            ],
            message_id=run_id,
        )
        receipt = dict(result.receipt)
        calls = list(receipt.get("tool_calls") or [])
        names = [row.get("name") for row in calls if isinstance(row, dict)]
        if names != ["bridge_status"]:
            raise RuntimeError("provider did not execute exactly one bridge_status call")
        output = root / ".peerbridge" / "receipts" / f"{receipt_name}.json"
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
            "provider_id": receipt["route"]["provider_id"],
            "requested_model_id": receipt["route"]["model_id"],
            "expected_response_model_id": receipt["route"]["response_model_id"],
            "observed_response_model_ids": receipt.get("observed_response_model_ids"),
            "tool_calls": names,
            "argument_formats": [row.get("arguments_format") for row in calls],
            "receipt_sha256": receipt.get("receipt_sha256"),
            "receipt_path": output.relative_to(root).as_posix(),
            "raw_content_recorded": receipt.get("raw_content_recorded"),
            "credential_contents_recorded": receipt.get("credential_contents_recorded"),
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
