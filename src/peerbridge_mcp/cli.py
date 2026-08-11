"""Command line entry point for PeerBridge MCP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .bridge import Bridge, BridgeError
from .server import serve


def _default_db(root: Path) -> Path:
    return root / ".peerbridge" / "peerbridge.sqlite3"


def _bridge_from_args(args: argparse.Namespace) -> Bridge:
    root = Path(args.project_root).resolve()
    db = Path(args.db).resolve() if args.db else _default_db(root)
    return Bridge(
        root,
        db,
        args.agent_id,
        args.scope,
        session_id=getattr(args, "session_id", None),
        client_name=getattr(args, "client_name", None),
        provider_id=getattr(args, "provider_id", None),
        model_id=getattr(args, "model_id", None),
        protected_paths=getattr(args, "protected_path", []) or [],
    )


def _write_config(root: Path, scope: str) -> dict[str, Any]:
    state = root / ".peerbridge"
    state.mkdir(parents=True, exist_ok=True)
    config = state / "config.json"
    if config.exists():
        data = json.loads(config.read_text(encoding="utf-8"))
        return {"created": False, "path": str(config), "config": data}
    data = {
        "scope": scope,
        "database": ".peerbridge/peerbridge.sqlite3",
        "protected_paths": [".git", ".peerbridge"],
    }
    config.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"created": True, "path": str(config), "config": data}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="peerbridge",
        description="Local-first MCP coordination for equal coding peers.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create .peerbridge configuration and database.")
    init.add_argument("--project-root", default=".")
    init.add_argument("--scope", default="default")

    for name, help_text in (
        ("serve", "Run the MCP server over stdio."),
        ("doctor", "Verify the local database and audit chain."),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--project-root", default=".")
        command.add_argument("--db")
        command.add_argument("--agent-id", required=name == "serve", default="doctor")
        command.add_argument("--scope", default="default")
        command.add_argument("--session-id")
        command.add_argument(
            "--client-name",
            help="Safe client label such as codex, claude-code or browser-adapter.",
        )
        command.add_argument(
            "--provider-id",
            help="Non-secret route label such as xai-official or relay-main.",
        )
        command.add_argument(
            "--model-id",
            help="Non-secret selected model label such as grok or deepseek.",
        )
        command.add_argument("--protected-path", action="append", default=[])

    monitor = sub.add_parser("monitor", help="Open the local pixel control room.")
    monitor.add_argument("--project-root", default=".")
    monitor.add_argument("--db")
    monitor.add_argument("--scope", default="default")
    monitor.add_argument("--refresh-ms", type=int, default=1500)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            root = Path(args.project_root).resolve()
            root.mkdir(parents=True, exist_ok=True)
            receipt = _write_config(root, args.scope)
            bridge = Bridge(root, _default_db(root), "initializer", args.scope)
            receipt["database"] = bridge.status()["database"]
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0
        if args.command == "serve":
            return serve(_bridge_from_args(args))
        if args.command == "doctor":
            bridge = _bridge_from_args(args)
            result = {
                "status": bridge.status(),
                "audit": bridge.verify_audit_chain(),
            }
            result["ok"] = bool(result["audit"]["valid"])
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1
        if args.command == "monitor":
            from .monitor import run_monitor

            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            return run_monitor(root, db, args.scope, args.refresh_ms)
    except (BridgeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"peerbridge: {exc}", file=sys.stderr)
        return 2
    return 2
