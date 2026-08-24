"""Command line entry point for PeerBridge MCP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .analytics import AnalyticsError, AnalyticsStore, record_launch
from .agent_identity import (
    AgentIdentityError,
    ensure_agent_identity_capability,
    verify_agent_identity_capability,
    verify_agent_identity_route_binding,
)
from .bridge import Bridge, BridgeError
from .doctor import inspect_database
from .execution_governance import ExecutionGovernance, GovernanceError
from .mailbox_supervisor import MailboxSupervisor, SupervisorError
from .product import (
    ProductConfigError,
    capability_manifest,
    capability_status,
    set_update_channel,
)
from .server import READ_ONLY_TOOLS, TOOL_SCHEMAS, serve


CAPABILITY_SAFE_TOOLS = frozenset(READ_ONLY_TOOLS) | {"hash_artifact"}
COLLABORATOR_TOOLS = CAPABILITY_SAFE_TOOLS | {
    "ack_message",
    "announce_work",
    "claim_task",
    "complete_task",
    "record_proof",
    "renew_task",
    "request_review",
    "send_message",
    "submit_patch",
    "submit_review",
}
RESERVED_IDENTITY_IDS = frozenset(
    {
        "human-operator",
        "control-room-workflow",
        "control-room-migrator",
        "mailbox-supervisor",
        "peerbridge-orchestrator",
    }
)


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
        reasoning_mode=getattr(args, "reasoning_mode", None),
        route_class=getattr(args, "route_class", None),
        protected_paths=getattr(args, "protected_path", []) or [],
    )


def _write_config(root: Path, scope: str, db_path: Path) -> dict[str, Any]:
    state = root / ".peerbridge"
    state.mkdir(parents=True, exist_ok=True)
    config = state / "config.json"
    if config.exists():
        data = json.loads(config.read_text(encoding="utf-8"))
        return {"created": False, "path": str(config), "config": data}
    try:
        database = db_path.relative_to(root).as_posix()
    except ValueError:
        database = str(db_path)
    data = {
        "scope": scope,
        "database": database,
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
    init.add_argument("--db")
    init.add_argument("--scope", default="default")

    migrate = sub.add_parser(
        "migrate", help="Explicitly upgrade an existing PeerBridge database."
    )
    migrate.add_argument("--project-root", default=".")
    migrate.add_argument("--db")
    migrate.add_argument("--scope", default="default")

    identity = sub.add_parser(
        "identity",
        help="Issue or revoke a local Agent identity capability.",
    )
    identity.add_argument("--project-root", default=".")
    identity.add_argument("--db")
    identity.add_argument("--scope", default="default")
    identity_actions = identity.add_subparsers(
        dest="identity_action",
        required=True,
    )
    identity_issue = identity_actions.add_parser("issue")
    identity_issue.add_argument("--agent-id", required=True)
    identity_issue.add_argument(
        "--profile",
        choices=("observer", "collaborator"),
        default="collaborator",
    )
    identity_issue.add_argument("--permission-decision-id", required=True)
    identity_revoke = identity_actions.add_parser("revoke")
    identity_revoke.add_argument("--capability-id", required=True)

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
        command.add_argument(
            "--reasoning-mode",
            help="Observed reasoning setting such as low, high, max, SOL or LUNA.",
        )
        command.add_argument(
            "--route-class",
            choices=("official", "relay", "local"),
            help="Observed provider class; required to satisfy a class-bound route.",
        )
        command.add_argument("--protected-path", action="append", default=[])
        if name == "serve":
            command.add_argument(
                "--identity-capability",
                required=True,
                help="Absolute path to a pre-issued local Agent identity capability.",
            )
            command.add_argument(
                "--allow-tool",
                action="append",
                default=[],
                help="Expose only this MCP tool; repeat for multiple tools.",
            )
            command.add_argument(
                "--allow-artifact-read",
                action="store_true",
                help=(
                    "Explicitly expose read_artifact. It is disabled by default because "
                    "returned project text is sent to the connected model provider."
                ),
            )

    monitor = sub.add_parser("monitor", help="Open the local pixel control room.")
    monitor.add_argument("--project-root", default=".")
    monitor.add_argument("--db")
    monitor.add_argument("--scope", default="default")
    monitor.add_argument("--refresh-ms", type=int, default=1500)

    remote = sub.add_parser(
        "remote",
        help="Run a loopback-only human control plane for Tailscale Serve.",
    )
    remote.add_argument("--project-root", default=".")
    remote.add_argument("--db")
    remote.add_argument("--scope", default="default")
    remote.add_argument("--host", default="127.0.0.1")
    remote.add_argument("--port", type=int, default=8765)
    remote.add_argument("--public-origin", required=True)
    remote.add_argument("--instance-id")
    remote.add_argument("--evidence-run-id")
    remote.add_argument("--evidence-minimum-gap-seconds", type=int, default=10)
    remote.add_argument(
        "--allow-login",
        action="append",
        default=[],
        help="Authorized Tailscale login; default is the signed-in tailnet owner.",
    )

    supervise = sub.add_parser(
        "supervise",
        help="Run the low-memory routed-message supervisor.",
    )
    supervise.add_argument("--project-root", default=".")
    supervise.add_argument("--db")
    supervise.add_argument("--scope", default="default")
    supervise.add_argument("--poll-seconds", type=float, default=5.0)
    supervise.add_argument("--lease-seconds", type=int, default=300)
    supervise.add_argument("--max-attempts", type=int, default=5)
    supervise.add_argument("--max-parallel-dispatches", type=int, default=16)
    supervise.add_argument(
        "--once",
        action="store_true",
        help="Run one discovery/dispatch cycle and exit.",
    )

    analytics = sub.add_parser(
        "analytics",
        help="Manage local-only, explicit opt-in aggregate analytics.",
    )
    analytics.add_argument("--project-root", default=".")
    analytics_actions = analytics.add_subparsers(dest="analytics_action", required=True)
    for action in ("status", "enable", "disable", "reset", "export"):
        analytics_actions.add_parser(action)
    analytics_record = analytics_actions.add_parser("record")
    analytics_record.add_argument("--event", required=True)
    analytics_record.add_argument(
        "--dimension",
        action="append",
        default=[],
        metavar="KEY=VALUE",
    )

    product = sub.add_parser(
        "product",
        help="Inspect public capabilities and dormant commercial boundaries.",
    )
    product.add_argument("--project-root", default=".")
    product_actions = product.add_subparsers(dest="product_action", required=True)
    product_status = product_actions.add_parser("status")
    product_status.add_argument("--capability")
    product_channel = product_actions.add_parser("set-channel")
    product_channel.add_argument(
        "--channel", choices=("stable", "beta", "experimental"), required=True
    )
    return parser


def _dimensions(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key.strip() or not item.strip():
            raise AnalyticsError("analytics dimensions must use KEY=VALUE")
        normalized = key.strip()
        if normalized in result:
            raise AnalyticsError("analytics dimensions cannot repeat a key")
        result[normalized] = item.strip()
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analytics":
            store = AnalyticsStore(Path(args.project_root))
            if args.analytics_action == "status":
                result = store.status()
            elif args.analytics_action == "enable":
                result = store.enable()
            elif args.analytics_action == "disable":
                result = store.disable()
            elif args.analytics_action == "reset":
                result = store.reset()
            elif args.analytics_action == "export":
                result = store.export()
            else:
                result = store.record(args.event, _dimensions(args.dimension))
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "product":
            root = Path(args.project_root).resolve()
            if args.product_action == "set-channel":
                result = set_update_channel(root, args.channel)
            elif args.capability:
                result = capability_status(root, args.capability)
            else:
                result = capability_manifest(root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "identity":
            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            if not root.is_dir() or not db.is_file():
                raise AgentIdentityError(
                    "PeerBridge workspace is unavailable; run peerbridge init first"
                )
            if args.identity_action != "issue":
                raise AgentIdentityError(
                    "identity revocation is available only to the authenticated Control Room"
                )
            if args.agent_id in RESERVED_IDENTITY_IDS or args.agent_id.startswith(
                "control-room-"
            ):
                raise AgentIdentityError(
                    "reserved operator identities cannot be issued by the Alpha CLI"
                )
            allowed_tools = (
                tuple(sorted(CAPABILITY_SAFE_TOOLS))
                if args.profile == "observer"
                else tuple(sorted(COLLABORATOR_TOOLS))
            )
            task_id = f"identity-issue:{args.agent_id}"
            action = "identity.capability.issue"
            resource_key = f"identity-profile:{args.profile}"
            governance = ExecutionGovernance(
                Bridge(
                    root,
                    db,
                    args.agent_id,
                    args.scope,
                    session_id=f"identity-issuer-{args.agent_id}",
                    client_name="peerbridge-identity-cli",
                )
            )
            governance.authorize_permission(
                args.permission_decision_id,
                task_id=task_id,
                agent_id=args.agent_id,
                action=action,
                resource_key=resource_key,
                consume=True,
            )
            capability = ensure_agent_identity_capability(
                root,
                db,
                args.scope,
                args.agent_id,
                allowed_tools=allowed_tools,
                issued_by=f"peerbridge-identity-cli-{args.profile}",
            )
            result = {
                "status": "issued",
                "agent_id": capability.agent_id,
                "scope": capability.scope,
                "profile": args.profile,
                "allowed_tools": list(capability.allowed_tools),
                "capability_id": capability.capability_id,
                "capability_sha256": capability.capability_sha256,
                "identity_capability": str(capability.path),
                "secret_contents_printed": False,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "serve":
            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            identity_capability = verify_agent_identity_capability(
                root,
                db,
                args.scope,
                args.agent_id,
                Path(args.identity_capability),
            )
            verify_agent_identity_route_binding(
                identity_capability,
                client_name=args.client_name,
                provider_id=args.provider_id,
                model_id=args.model_id,
                reasoning_mode=args.reasoning_mode,
                route_class=args.route_class,
            )
            if args.allow_artifact_read:
                raise BridgeError(
                    "capability-backed Agent serve cannot enable artifact reads"
                )
            requested_tools = frozenset(args.allow_tool)
            known_tools = frozenset(str(tool["name"]) for tool in TOOL_SCHEMAS)
            unknown_tools = requested_tools - known_tools
            if unknown_tools:
                raise BridgeError(
                    "capability-backed Agent serve requested unknown tools: "
                    + ", ".join(sorted(unknown_tools))
                )
            if "read_artifact" in requested_tools:
                raise BridgeError(
                    "capability-backed Agent serve cannot enable artifact reads"
                )
            capability_tools = (
                CAPABILITY_SAFE_TOOLS
                if identity_capability.uses_legacy_tool_fallback
                else frozenset(identity_capability.allowed_tools)
            )
            if not requested_tools:
                requested_tools = capability_tools
            unsafe_tools = requested_tools - capability_tools
            if unsafe_tools:
                raise BridgeError(
                    "capability-backed Agent serve requested tools outside its "
                    "bound allowlist: "
                    + ", ".join(sorted(unsafe_tools))
                )
        launch_features = {
            "serve": "local_core",
            "monitor": "control_room",
            "remote": "experimental_remote",
            "supervise": "local_core",
        }
        if args.command in launch_features:
            record_launch(Path(args.project_root), launch_features[args.command])
        if args.command == "init":
            root = Path(args.project_root).resolve()
            root.mkdir(parents=True, exist_ok=True)
            db = Path(args.db).resolve() if args.db else _default_db(root)
            receipt = _write_config(root, args.scope, db)
            bridge = Bridge(root, db, "initializer", args.scope)
            receipt["database"] = bridge.status()["database"]
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0
        if args.command == "migrate":
            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            if not root.is_dir():
                raise BridgeError(f"project root does not exist: {root}")
            if not db.is_file():
                raise BridgeError(
                    f"database does not exist: {db}; run peerbridge init first"
                )
            bridge = Bridge(root, db, "migrator", args.scope)
            status = bridge.status()
            result = {
                "database": status["database"],
                "schema_version": status["schema_version"],
                "writes_performed": True,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "serve":
            allowed_tools = requested_tools
            denied_tools = frozenset(
                {"read_artifact", "list_rooms", "read_memory"}
                if identity_capability.bound_room_id is not None
                else {"read_artifact"}
            )
            return serve(
                _bridge_from_args(args),
                allowed_tools=allowed_tools,
                denied_tools=denied_tools,
                identity_capability=identity_capability,
            )
        if args.command == "doctor":
            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            result = inspect_database(root, db, args.scope)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1
        if args.command == "monitor":
            from .monitor import run_monitor

            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            return run_monitor(root, db, args.scope, args.refresh_ms)
        if args.command == "remote":
            from .remote import run_remote

            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            return run_remote(
                root,
                db,
                args.scope,
                args.host,
                args.port,
                set(args.allow_login),
                args.public_origin,
                args.instance_id,
                args.evidence_run_id,
                args.evidence_minimum_gap_seconds,
            )
        if args.command == "supervise":
            root = Path(args.project_root).resolve()
            db = Path(args.db).resolve() if args.db else _default_db(root)
            supervisor = MailboxSupervisor(
                root,
                db,
                args.scope,
                lease_seconds=args.lease_seconds,
                max_attempts=args.max_attempts,
                max_parallel_dispatches=args.max_parallel_dispatches,
            )
            if args.once:
                result = supervisor.run_once()
                print(json.dumps(result.__dict__, indent=2, sort_keys=True))
                return 0
            supervisor.run_forever(args.poll_seconds)
            return 0
    except (
        AgentIdentityError,
        GovernanceError,
        BridgeError,
        AnalyticsError,
        ProductConfigError,
        SupervisorError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"peerbridge: {exc}", file=sys.stderr)
        return 2
    return 2


def supervisor_main() -> int:
    """Console-script wrapper that selects the supervise subcommand."""
    return main(["supervise", *sys.argv[1:]])
