"""Localized Control Room surface for Alpha 5.2 trust and workflow controls."""

from __future__ import annotations

import json
import re
import time
import tkinter as tk
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any, Callable, Iterable, Mapping

from .execution_governance import repository_resource_key
from .operation_queue import WORKFLOW_TEMPLATES
from .secret_scan import redact_secrets


WORKFLOW_IDS = (
    "implement-review",
    "investigate-debate",
    "read-only-audit",
    "release-gate",
)
GENERIC_TRUST_STAGES = (
    "claim",
    "execution",
    "test",
    "proof",
    "review",
    "decision",
)
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")

ToolCallback = Callable[[dict[str, Any] | None, str | None], None]
ToolExecutor = Callable[[str, dict[str, Any], ToolCallback], None]


def split_control_values(value: str) -> list[str]:
    """Split compact comma/newline fields without changing order."""

    result: list[str] = []
    for item in re.split(r"[,;\n]", str(value or "")):
        text = item.strip()
        if text and text not in result:
            result.append(text)
    return result


def proof_bundle_output_path(task_id: str, now: datetime | None = None) -> str:
    """Return a unique project-relative create-only Proof Bundle path."""

    task = str(task_id or "").strip()
    if not SAFE_ID.fullmatch(task):
        raise ValueError("invalid task id")
    observed = now or datetime.now(timezone.utc)
    stamp = observed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f".peerbridge-artifacts/proof-bundles/{task}-{stamp}-{uuid.uuid4().hex[:8]}"


class TrustWorkflowsPage:
    """Human-operated local queue, trust, governance, and proof surface."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        project_root: Path,
        translate: Callable[[str], str],
        colors: Mapping[str, str],
        execute_tool: ToolExecutor,
    ) -> None:
        self.project_root = project_root.resolve()
        self._t = translate
        self.colors = colors
        self.execute_tool = execute_tool
        self.frame = tk.Frame(parent, bg=colors["bg"])
        self.frame.grid(row=0, column=0, sticky="nsew")
        self.frame.grid_rowconfigure(1, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.status = tk.StringVar()
        self.verification_status = tk.StringVar()
        self.release_status = tk.StringVar()
        self._busy = False
        self._labels: list[tuple[tk.Label, str]] = []
        self._buttons: list[tuple[tk.Button, str]] = []
        self._tabs: list[tuple[ttk.Notebook, tk.Widget, str]] = []
        self._headings: dict[str, tuple[tuple[str, str], ...]] = {}
        self._trees: dict[str, ttk.Treeview] = {}
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._action_buttons: list[tk.Button] = []
        self._snapshot: Any = None
        self._workflow_labels = {value: value for value in WORKFLOW_IDS}
        self._stage_labels = {value: value for value in GENERIC_TRUST_STAGES}
        self._severity_labels = {
            value: value for value in ("low", "medium", "high", "critical")
        }
        self._permission_labels = {value: value for value in ("allow", "deny")}
        self._verification_status_provider: Callable[[], Mapping[str, Any]] | None = None
        self._request_verification_scan: Callable[[], None] | None = None
        self._release_fingerprint = ""
        self._last_release_result: dict[str, Any] | None = None
        self._build_header()
        self._build_notebook()
        self.apply_locale()

    def _build_header(self) -> None:
        header = tk.Frame(self.frame, bg=self.colors["panel"], bd=2, relief="ridge")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(0, weight=1)
        status = tk.Label(
            header,
            textvariable=self.status,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Cascadia Mono", 8, "bold"),
        )
        status.grid(row=0, column=0, sticky="ew", padx=12, pady=7)
        self.status_label = status
        badge = tk.Label(
            header,
            bg=self.colors["green"],
            fg=self.colors["black"],
            padx=8,
            pady=3,
            font=("Cascadia Mono", 8, "bold"),
        )
        badge.grid(row=0, column=1, padx=8, pady=5)
        self.local_badge = badge
        engine = tk.Label(
            header,
            textvariable=self.verification_status,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Cascadia Mono", 9, "bold"),
        )
        engine.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 7))
        self.verification_status_label = engine
        scan = self._button(
            header,
            "trustui.action.scan_now",
            self._scan_verification_now,
            color="blue",
        )
        scan.grid(row=1, column=1, padx=8, pady=(0, 7))
        self.verification_scan_button = scan

    def _build_notebook(self) -> None:
        notebook = ttk.Notebook(self.frame)
        notebook.grid(row=1, column=0, sticky="nsew")
        self.notebook = notebook
        self.operations_tab = self._tab(notebook, "trustui.tab.operations")
        self.schedules_tab = self._tab(notebook, "trustui.tab.schedules")
        self.timeline_tab = self._tab(notebook, "trustui.tab.timeline")
        self.governance_tab = self._tab(notebook, "trustui.tab.governance")
        self.proof_tab = self._tab(notebook, "trustui.tab.proof")
        self._build_operations()
        self._build_schedules()
        self._build_timeline()
        self._build_governance()
        self._build_proof()

    def _tab(self, notebook: ttk.Notebook, key: str) -> tk.Frame:
        tab = tk.Frame(notebook, bg=self.colors["panel"], padx=8, pady=8)
        notebook.add(tab, text=key)
        self._tabs.append((notebook, tab, key))
        return tab

    def _label(
        self,
        parent: tk.Misc,
        key: str,
        *,
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> tk.Label:
        label = tk.Label(
            parent,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Cascadia Mono", 8, "bold"),
        )
        label.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="w",
            padx=(0, 6),
            pady=(2, 1),
        )
        self._labels.append((label, key))
        return label

    def _entry(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        *,
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> tk.Entry:
        entry = tk.Entry(
            parent,
            textvariable=variable,
            bg=self.colors["black"],
            fg=self.colors["text"],
            insertbackground=self.colors["cyan"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
        )
        entry.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=(0, 8),
            pady=(0, 5),
            ipady=3,
        )
        return entry

    def _button(
        self,
        parent: tk.Misc,
        key: str,
        command: Callable[[], None],
        *,
        color: str = "line",
    ) -> tk.Button:
        button = tk.Button(
            parent,
            command=command,
            bg=self.colors[color],
            fg=self.colors["black"] if color in {"cyan", "green", "amber", "blue"} else self.colors["text"],
            activebackground=self.colors["cyan"],
            activeforeground=self.colors["black"],
            relief="raised",
            bd=2,
            padx=8,
            pady=4,
            font=("Cascadia Mono", 8, "bold"),
        )
        self._buttons.append((button, key))
        self._action_buttons.append(button)
        return button

    def _tree(
        self,
        parent: tk.Misc,
        name: str,
        columns: Iterable[tuple[str, str, int]],
        *,
        row: int,
        column: int = 0,
        columnspan: int = 1,
        selectmode: str = "browse",
    ) -> ttk.Treeview:
        definitions = tuple(columns)
        host = tk.Frame(parent, bg=self.colors["panel"])
        host.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="nsew",
            pady=(5, 0),
        )
        host.grid_rowconfigure(0, weight=1)
        host.grid_columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            host,
            columns=[item[0] for item in definitions],
            show="headings",
            selectmode=selectmode,
        )
        for key, heading_key, width in definitions:
            tree.heading(key, text=heading_key)
            tree.column(key, width=width, minwidth=65, stretch=True)
        yscroll = ttk.Scrollbar(host, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(host, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        tree.bind("<Double-1>", lambda _event, value=name: self._show_detail(value))
        self._trees[name] = tree
        self._records[name] = {}
        self._headings[name] = tuple((item[0], item[1]) for item in definitions)
        return tree

    def _build_operations(self) -> None:
        tab = self.operations_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        tab.grid_rowconfigure(5, weight=1)
        self.operation_workflow = tk.StringVar(value=WORKFLOW_IDS[0])
        self.operation_task = tk.StringVar()
        self.operation_directory = tk.StringVar(value=".")
        self.operation_resource = tk.StringVar(value="repo:main")
        self.operation_permission = tk.StringVar()
        self._label(tab, "trustui.field.workflow", row=0, column=0)
        workflow = ttk.Combobox(
            tab,
            textvariable=self.operation_workflow,
            values=WORKFLOW_IDS,
            state="readonly",
            width=22,
        )
        self.operation_workflow_combo = workflow
        workflow.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(2, 5))
        self._label(tab, "trustui.field.directory", row=0, column=2)
        self._entry(tab, self.operation_directory, row=0, column=3)
        self._label(tab, "trustui.field.task", row=1, column=0)
        self._entry(tab, self.operation_task, row=1, column=1, columnspan=3)
        self._label(tab, "trustui.field.resource", row=2, column=0)
        self._entry(tab, self.operation_resource, row=2, column=1)
        self._label(tab, "trustui.field.permission", row=2, column=2)
        self._entry(tab, self.operation_permission, row=2, column=3)
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew")
        self._button(
            actions,
            "trustui.action.enqueue",
            self._enqueue_operation,
            color="green",
        ).pack(side="left", padx=(0, 5))
        self._button(actions, "trustui.action.cancel", self._cancel_operation).pack(
            side="left", padx=5
        )
        self._button(
            actions, "trustui.action.reconcile", self._reconcile_operations
        ).pack(side="left", padx=5)
        release = tk.Frame(tab, bg=self.colors["panel"], bd=1, relief="ridge")
        release.grid(
            row=4,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(6, 0),
            padx=(0, 8),
        )
        release.grid_columnconfigure(1, weight=1)
        self.release_reason = tk.StringVar()
        self._label(release, "trustui.field.release_reason", row=0, column=0)
        self._entry(release, self.release_reason, row=0, column=1, columnspan=3)
        release_actions = tk.Frame(release, bg=self.colors["panel"])
        release_actions.grid(row=1, column=0, columnspan=4, sticky="ew", padx=6)
        self._button(
            release_actions,
            "trustui.action.request_release_gate",
            self._request_release_gate,
            color="green",
        ).pack(side="left", padx=(0, 4))
        self._button(
            release_actions,
            "trustui.action.check_release_gate",
            self._check_release_gate,
        ).pack(side="left", padx=4)
        self._button(
            release_actions,
            "trustui.action.approve_release_gate",
            lambda: self._decide_release_gate("approve"),
            color="blue",
        ).pack(side="left", padx=4)
        self._button(
            release_actions,
            "trustui.action.reject_release_gate",
            lambda: self._decide_release_gate("reject"),
        ).pack(side="left", padx=4)
        release_status = tk.Label(
            release,
            textvariable=self.release_status,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Cascadia Mono", 9, "bold"),
        )
        release_status.grid(
            row=2, column=0, columnspan=4, sticky="ew", padx=8, pady=(4, 6)
        )
        self.release_status_label = release_status
        self.operation_tree = self._tree(
            tab,
            "operations",
            (
                ("operation", "trustui.heading.operation", 180),
                ("workflow", "trustui.heading.workflow", 130),
                ("status", "trustui.heading.status", 90),
                ("attempts", "trustui.heading.attempts", 75),
                ("updated", "trustui.heading.updated", 130),
            ),
            row=5,
            columnspan=4,
        )

    def _build_schedules(self) -> None:
        tab = self.schedules_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self.schedule_workflow = tk.StringVar(value=WORKFLOW_IDS[3])
        self.schedule_task = tk.StringVar()
        self.schedule_directory = tk.StringVar(value=".")
        self.schedule_resource = tk.StringVar(value="repo:release")
        self.schedule_interval = tk.StringVar(value="60")
        self.schedule_enabled = tk.BooleanVar(value=False)
        self._label(tab, "trustui.field.workflow", row=0, column=0)
        workflow = ttk.Combobox(
            tab,
            textvariable=self.schedule_workflow,
            values=WORKFLOW_IDS,
            state="readonly",
        )
        self.schedule_workflow_combo = workflow
        workflow.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(2, 5))
        self._label(tab, "trustui.field.interval", row=0, column=2)
        self._entry(tab, self.schedule_interval, row=0, column=3)
        self._label(tab, "trustui.field.task", row=1, column=0)
        self._entry(tab, self.schedule_task, row=1, column=1, columnspan=3)
        self._label(tab, "trustui.field.directory", row=2, column=0)
        self._entry(tab, self.schedule_directory, row=2, column=1)
        self._label(tab, "trustui.field.resource", row=2, column=2)
        self._entry(tab, self.schedule_resource, row=2, column=3)
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.schedule_toggle = tk.Checkbutton(
            actions,
            variable=self.schedule_enabled,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            activebackground=self.colors["panel"],
            activeforeground=self.colors["text"],
            selectcolor=self.colors["black"],
            font=("Cascadia Mono", 8, "bold"),
        )
        self.schedule_toggle.pack(side="left", padx=(0, 8))
        self._button(
            actions,
            "trustui.action.save_schedule",
            self._save_schedule,
            color="green",
        ).pack(side="left", padx=4)
        self._button(actions, "trustui.action.enable", lambda: self._toggle_schedule(True)).pack(
            side="left", padx=4
        )
        self._button(actions, "trustui.action.disable", lambda: self._toggle_schedule(False)).pack(
            side="left", padx=4
        )
        self._button(
            actions, "trustui.action.materialize", self._materialize_schedules
        ).pack(side="left", padx=4)
        self.schedule_tree = self._tree(
            tab,
            "schedules",
            (
                ("schedule", "trustui.heading.schedule", 170),
                ("workflow", "trustui.heading.workflow", 130),
                ("enabled", "trustui.heading.enabled", 75),
                ("next", "trustui.heading.next", 130),
                ("updated", "trustui.heading.updated", 125),
            ),
            row=4,
            columnspan=4,
        )

    def _build_timeline(self) -> None:
        tab = self.timeline_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self.trust_task = tk.StringVar()
        self.trust_stage = tk.StringVar(value="claim")
        self.trust_statement = tk.StringVar()
        self.trust_artifacts = tk.StringVar()
        self._label(tab, "trustui.field.task", row=0, column=0)
        self._entry(tab, self.trust_task, row=0, column=1)
        self._label(tab, "trustui.field.stage", row=0, column=2)
        stage = ttk.Combobox(
            tab,
            textvariable=self.trust_stage,
            values=GENERIC_TRUST_STAGES,
            state="readonly",
        )
        self.trust_stage_combo = stage
        stage.grid(row=0, column=3, sticky="ew", padx=(0, 8), pady=(2, 5))
        self._label(tab, "trustui.field.statement", row=1, column=0)
        self._entry(tab, self.trust_statement, row=1, column=1, columnspan=3)
        self._label(tab, "trustui.field.artifacts", row=2, column=0)
        self._entry(tab, self.trust_artifacts, row=2, column=1, columnspan=3)
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew")
        self._button(
            actions, "trustui.action.record", self._record_trust, color="green"
        ).pack(side="left", padx=(0, 4))
        self._button(
            actions, "trustui.action.disagreement", self._record_disagreement
        ).pack(side="left", padx=4)
        self._button(actions, "trustui.action.recheck", self._recheck_trust).pack(
            side="left", padx=4
        )
        self._button(
            actions, "trustui.action.complete", self._complete_trust, color="amber"
        ).pack(side="left", padx=4)
        self.trust_tree = self._tree(
            tab,
            "trust",
            (
                ("stage", "trustui.heading.stage", 85),
                ("task", "trustui.heading.task", 145),
                ("actor", "trustui.heading.actor", 110),
                ("freshness", "trustui.heading.freshness", 90),
                ("statement", "trustui.heading.statement", 280),
                ("time", "trustui.heading.time", 125),
            ),
            row=4,
            columnspan=4,
            selectmode="extended",
        )

    def _build_governance(self) -> None:
        tab = self.governance_tab
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        notebook = ttk.Notebook(tab)
        notebook.grid(row=0, column=0, sticky="nsew")
        permissions = self._tab(notebook, "trustui.tab.permissions")
        worktrees = self._tab(notebook, "trustui.tab.worktrees")
        capabilities = self._tab(notebook, "trustui.tab.capabilities")
        decisions = self._tab(notebook, "trustui.tab.decisions")
        self._build_permissions(permissions)
        self._build_worktrees(worktrees)
        self._build_capabilities(capabilities)
        self._build_decisions(decisions)

    def _build_permissions(self, tab: tk.Frame) -> None:
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self.permission_task = tk.StringVar()
        self.permission_agent = tk.StringVar(value="codex")
        self.permission_action = tk.StringVar(value="git.worktree.create")
        self.permission_resource = tk.StringVar(
            value=repository_resource_key(self.project_root)
        )
        self.permission_decision = tk.StringVar()
        self.permission_reason = tk.StringVar()
        self.permission_expiry = tk.StringVar(value="15")
        self._label(tab, "trustui.field.task", row=0, column=0)
        self._entry(tab, self.permission_task, row=0, column=1)
        self._label(tab, "trustui.field.agent", row=0, column=2)
        self._entry(tab, self.permission_agent, row=0, column=3)
        self._label(tab, "trustui.field.action", row=1, column=0)
        self._entry(tab, self.permission_action, row=1, column=1)
        self._label(tab, "trustui.field.resource", row=1, column=2)
        self._entry(tab, self.permission_resource, row=1, column=3)
        self._label(tab, "trustui.field.reason", row=2, column=0)
        self._entry(tab, self.permission_reason, row=2, column=1)
        self._label(tab, "trustui.field.expires", row=2, column=2)
        expiry_host = tk.Frame(tab, bg=self.colors["panel"])
        expiry_host.grid(row=2, column=3, sticky="ew", padx=(0, 8), pady=(0, 5))
        expiry_host.grid_columnconfigure(1, weight=1)
        self.permission_choice = ttk.Combobox(
            expiry_host,
            textvariable=self.permission_decision,
            state="readonly",
            width=10,
        )
        self.permission_choice.grid(row=0, column=0, sticky="w", padx=(0, 5))
        tk.Entry(
            expiry_host,
            textvariable=self.permission_expiry,
            width=8,
            bg=self.colors["black"],
            fg=self.colors["text"],
            insertbackground=self.colors["cyan"],
            font=("Cascadia Mono", 9),
        ).grid(row=0, column=1, sticky="ew", ipady=3)
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew")
        self._button(
            actions, "trustui.action.decide", self._decide_permission, color="amber"
        ).pack(side="left")
        tree = self._tree(
            tab,
            "permissions",
            (
                ("decision", "trustui.heading.decision", 150),
                ("task", "trustui.heading.task", 120),
                ("agent", "trustui.heading.agent", 100),
                ("action", "trustui.heading.action", 140),
                ("result", "trustui.heading.result", 70),
                ("expires", "trustui.heading.expires", 120),
            ),
            row=4,
            columnspan=4,
        )
        tree.bind("<<TreeviewSelect>>", self._permission_selected, add="+")

    def _build_worktrees(self, tab: tk.Frame) -> None:
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self.worktree_binding = tk.StringVar()
        self.worktree_task = tk.StringVar()
        self.worktree_agent = tk.StringVar(value="codex")
        self.worktree_permission = tk.StringVar()
        self.worktree_repository = tk.StringVar(value=".")
        self.worktree_base = tk.StringVar(value="HEAD")
        self._label(tab, "trustui.field.binding", row=0, column=0)
        self._entry(tab, self.worktree_binding, row=0, column=1)
        self._label(tab, "trustui.field.permission", row=0, column=2)
        self._entry(tab, self.worktree_permission, row=0, column=3)
        self._label(tab, "trustui.field.task", row=1, column=0)
        self._entry(tab, self.worktree_task, row=1, column=1)
        self._label(tab, "trustui.field.agent", row=1, column=2)
        self._entry(tab, self.worktree_agent, row=1, column=3)
        self._label(tab, "trustui.field.repository", row=2, column=0)
        self._entry(tab, self.worktree_repository, row=2, column=1)
        self._label(tab, "trustui.field.base", row=2, column=2)
        self._entry(tab, self.worktree_base, row=2, column=3)
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew")
        self._button(
            actions,
            "trustui.action.create_worktree",
            self._create_worktree,
            color="amber",
        ).pack(side="left", padx=(0, 4))
        self._button(actions, "trustui.action.seal", self._seal_worktree).pack(
            side="left", padx=4
        )
        self._button(
            actions, "trustui.action.verify_source", self._verify_worktree
        ).pack(side="left", padx=4)
        tree = self._tree(
            tab,
            "worktrees",
            (
                ("binding", "trustui.heading.binding", 150),
                ("task", "trustui.heading.task", 120),
                ("agent", "trustui.heading.agent", 100),
                ("state", "trustui.heading.state", 75),
                ("commit", "trustui.heading.commit", 130),
                ("diff", "trustui.heading.diff", 120),
            ),
            row=4,
            columnspan=4,
        )
        tree.bind("<<TreeviewSelect>>", self._worktree_selected, add="+")

    def _build_capabilities(self, tab: tk.Frame) -> None:
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        self._tree(
            tab,
            "capabilities",
            (
                ("capability", "trustui.heading.capability", 180),
                ("version", "trustui.heading.version", 90),
                ("kind", "trustui.heading.kind", 90),
                ("sensitivity", "trustui.heading.sensitivity", 90),
                ("enabled", "trustui.heading.enabled", 75),
                ("hash", "trustui.heading.hash", 110),
            ),
            row=0,
        )
        self._tree(
            tab,
            "grants",
            (
                ("grant", "trustui.heading.grant", 160),
                ("principal", "trustui.heading.principal", 130),
                ("capability", "trustui.heading.capability", 165),
                ("result", "trustui.heading.result", 75),
                ("time", "trustui.heading.time", 120),
            ),
            row=1,
        )

    def _build_decisions(self, tab: tk.Frame) -> None:
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self.briefing_task = tk.StringVar()
        self.briefing_room = tk.StringVar()
        self.briefing_applicability = tk.StringVar()
        self.conflict_memory_ids = tk.StringVar()
        self.conflict_summary = tk.StringVar()
        self.conflict_severity = tk.StringVar(value="medium")
        self._label(tab, "trustui.field.task", row=0, column=0)
        self._entry(tab, self.briefing_task, row=0, column=1)
        self._label(tab, "trustui.field.room", row=0, column=2)
        self._entry(tab, self.briefing_room, row=0, column=3)
        self._label(tab, "trustui.field.applicability", row=1, column=0)
        self._entry(tab, self.briefing_applicability, row=1, column=1)
        self._label(tab, "trustui.field.memory_ids", row=1, column=2)
        self._entry(tab, self.conflict_memory_ids, row=1, column=3)
        self._label(tab, "trustui.field.summary", row=2, column=0)
        self._entry(tab, self.conflict_summary, row=2, column=1)
        self._label(tab, "trustui.field.severity", row=2, column=2)
        severity = ttk.Combobox(
            tab,
            textvariable=self.conflict_severity,
            values=("low", "medium", "high", "critical"),
            state="readonly",
        )
        self.conflict_severity_combo = severity
        severity.grid(row=2, column=3, sticky="ew", padx=(0, 8), pady=(0, 5))
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew")
        self._button(
            actions, "trustui.action.brief", self._brief_task, color="green"
        ).pack(side="left", padx=(0, 4))
        self._button(
            actions, "trustui.action.record_conflict", self._record_conflict
        ).pack(side="left", padx=4)
        split = ttk.Panedwindow(tab, orient="vertical")
        split.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(5, 0))
        brief_host = tk.Frame(split, bg=self.colors["panel"])
        conflict_host = tk.Frame(split, bg=self.colors["panel"])
        brief_host.grid_rowconfigure(0, weight=1)
        brief_host.grid_columnconfigure(0, weight=1)
        conflict_host.grid_rowconfigure(0, weight=1)
        conflict_host.grid_columnconfigure(0, weight=1)
        split.add(brief_host, weight=1)
        split.add(conflict_host, weight=1)
        tree = self._tree(
            brief_host,
            "briefings",
            (
                ("briefing", "trustui.heading.briefing", 155),
                ("task", "trustui.heading.task", 125),
                ("agent", "trustui.heading.agent", 100),
                ("records", "trustui.heading.records", 75),
                ("time", "trustui.heading.time", 120),
            ),
            row=0,
        )
        tree.bind("<<TreeviewSelect>>", self._briefing_selected, add="+")
        self._tree(
            conflict_host,
            "conflicts",
            (
                ("conflict", "trustui.heading.conflict", 155),
                ("task", "trustui.heading.task", 125),
                ("severity", "trustui.heading.severity", 75),
                ("summary", "trustui.heading.summary", 260),
                ("time", "trustui.heading.time", 120),
            ),
            row=0,
        )

    def _build_proof(self) -> None:
        tab = self.proof_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(4, weight=1)
        self.proof_task = tk.StringVar()
        self.proof_output = tk.StringVar()
        self.proof_bundle = tk.StringVar()
        self.proof_result = tk.StringVar()
        self._label(tab, "trustui.field.task", row=0, column=0)
        self._entry(tab, self.proof_task, row=0, column=1)
        self._label(tab, "trustui.field.output", row=1, column=0)
        self._entry(tab, self.proof_output, row=1, column=1)
        self._label(tab, "trustui.field.bundle", row=2, column=0)
        self._entry(tab, self.proof_bundle, row=2, column=1)
        actions = tk.Frame(tab, bg=self.colors["panel"])
        actions.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._button(
            actions, "trustui.action.export", self._export_proof, color="green"
        ).pack(side="left", padx=(0, 4))
        self._button(actions, "trustui.action.choose", self._choose_proof).pack(
            side="left", padx=4
        )
        self._button(
            actions, "trustui.action.verify", self._verify_proof, color="blue"
        ).pack(side="left", padx=4)
        result = tk.Text(
            tab,
            height=12,
            wrap="word",
            bg=self.colors["black"],
            fg=self.colors["text"],
            relief="sunken",
            bd=2,
            font=("Cascadia Mono", 9),
            padx=10,
            pady=8,
        )
        result.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        result.configure(state="disabled")
        self.proof_result_widget = result

    def apply_locale(self) -> None:
        operation_workflow = self._choice_id(
            self.operation_workflow.get(), self._workflow_labels, WORKFLOW_IDS[0]
        )
        schedule_workflow = self._choice_id(
            self.schedule_workflow.get(), self._workflow_labels, WORKFLOW_IDS[-1]
        )
        trust_stage = self._choice_id(
            self.trust_stage.get(), self._stage_labels, GENERIC_TRUST_STAGES[0]
        )
        conflict_severity = self._choice_id(
            self.conflict_severity.get(), self._severity_labels, "medium"
        )
        permission_decision = self._choice_id(
            self.permission_decision.get(), self._permission_labels, "allow"
        )
        self._workflow_labels = {
            self._t(f"trustui.workflow.{value}"): value for value in WORKFLOW_IDS
        }
        self._stage_labels = {
            self._t(f"trustui.stage.{value}"): value for value in GENERIC_TRUST_STAGES
        }
        self._severity_labels = {
            self._t(f"trustui.severity.{value}"): value
            for value in ("low", "medium", "high", "critical")
        }
        for combo in (self.operation_workflow_combo, self.schedule_workflow_combo):
            combo.configure(values=tuple(self._workflow_labels))
        self.trust_stage_combo.configure(values=tuple(self._stage_labels))
        self.conflict_severity_combo.configure(values=tuple(self._severity_labels))
        self.operation_workflow.set(
            self._choice_label(operation_workflow, self._workflow_labels)
        )
        self.schedule_workflow.set(
            self._choice_label(schedule_workflow, self._workflow_labels)
        )
        self.trust_stage.set(self._choice_label(trust_stage, self._stage_labels))
        self.conflict_severity.set(
            self._choice_label(conflict_severity, self._severity_labels)
        )
        self.local_badge.configure(text=self._t("trustui.local_only"))
        if not self._busy:
            self.status.set(self._t("trustui.status.ready"))
        for label, key in self._labels:
            label.configure(text=self._t(key))
        for button, key in self._buttons:
            button.configure(text=self._t(key))
        for notebook, tab, key in self._tabs:
            notebook.tab(tab, text=self._t(key))
        for name, headings in self._headings.items():
            tree = self._trees[name]
            for column, key in headings:
                tree.heading(column, text=self._t(key))
        self._permission_labels = {
            self._t("trustui.choice.allow"): "allow",
            self._t("trustui.choice.deny"): "deny",
        }
        self.permission_choice.configure(values=tuple(self._permission_labels))
        self.permission_decision.set(
            self._choice_label(permission_decision, self._permission_labels)
        )
        self.schedule_toggle.configure(text=self._t("trustui.field.enabled"))
        self.refresh_verification_status()
        if self._last_release_result is not None:
            self._render_release_gate(self._last_release_result)
        elif not self.release_status.get():
            self.release_status.set(self._t("trustui.release.not_requested"))

    def set_verification_callbacks(
        self,
        *,
        status_provider: Callable[[], Mapping[str, Any]],
        request_scan: Callable[[], None],
    ) -> None:
        self._verification_status_provider = status_provider
        self._request_verification_scan = request_scan
        self.refresh_verification_status()

    def refresh_verification_status(self) -> None:
        if self._verification_status_provider is None:
            self.verification_status.set(self._t("trustui.engine.not_started"))
            self.verification_status_label.configure(fg=self.colors["muted"])
            return
        try:
            status = dict(self._verification_status_provider())
        except Exception:
            self.verification_status.set(self._t("trustui.engine.unavailable"))
            self.verification_status_label.configure(fg=self.colors["red"])
            return
        state = str(status.get("state") or "not-started")
        counts = status.get("last_counts") or {}
        if state == "error":
            self.verification_status.set(
                self._t("trustui.engine.error").format(
                    error=str(status.get("last_error") or "unknown")[:120]
                )
            )
            color = self.colors["red"]
        elif status.get("thread_alive") and state in {"starting", "running"}:
            self.verification_status.set(
                self._t("trustui.engine.running").format(
                    scans=int(status.get("scan_count") or 0),
                    created=int(counts.get("created") or 0),
                    open=int(counts.get("open_automatic") or 0),
                )
            )
            color = self.colors["green"]
        elif state == "stopped":
            self.verification_status.set(self._t("trustui.engine.stopped"))
            color = self.colors["red"]
        else:
            self.verification_status.set(self._t("trustui.engine.starting"))
            color = self.colors["amber"]
        self.verification_status_label.configure(fg=color)

    def _scan_verification_now(self) -> None:
        if self._request_verification_scan is None:
            self.verification_status.set(self._t("trustui.engine.unavailable"))
            self.verification_status_label.configure(fg=self.colors["red"])
            return
        self._request_verification_scan()
        self.verification_status.set(self._t("trustui.engine.scan_requested"))
        self.verification_status_label.configure(fg=self.colors["amber"])

    @staticmethod
    def _choice_id(value: str, labels: Mapping[str, str], default: str) -> str:
        selected = str(value or "")
        if selected in labels:
            return labels[selected]
        if selected in labels.values():
            return selected
        return default

    @staticmethod
    def _choice_label(value: str, labels: Mapping[str, str]) -> str:
        return next((label for label, item in labels.items() if item == value), value)

    def _localized_value(self, prefix: str, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        known = {
            "workflow": set(WORKFLOW_IDS),
            "stage": set(GENERIC_TRUST_STAGES),
            "severity": {"low", "medium", "high", "critical"},
            "status": {
                "queued",
                "running",
                "retry",
                "cancelling",
                "cancelled",
                "succeeded",
                "failed",
                "active",
                "sealed",
                "allow",
                "deny",
            },
        }
        if normalized in known.get(prefix, set()):
            key_prefix = "trustui.value" if prefix == "status" else f"trustui.{prefix}"
            return self._t(f"{key_prefix}.{normalized}")
        return normalized or "--"

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        state = "disabled" if busy else "normal"
        for button in self._action_buttons:
            button.configure(state=state)

    def _invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        on_success: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if self._busy:
            self.status.set(self._t("trustui.status.busy"))
            return
        self.set_busy(True)
        self.status_label.configure(fg=self.colors["amber"])
        self.status.set(self._t("trustui.status.running").format(tool=tool))

        def finished(result: dict[str, Any] | None, error: str | None) -> None:
            self.set_busy(False)
            if error is not None:
                self.status_label.configure(fg=self.colors["red"])
                self.status.set(self._t("trustui.status.error").format(error=error))
                return
            assert result is not None
            if on_success is not None:
                on_success(result)
            self.status_label.configure(fg=self.colors["green"])
            self.status.set(self._t("trustui.status.success").format(tool=tool))

        self.execute_tool(tool, arguments, finished)

    def _input_error(self, field_key: str) -> None:
        self.status_label.configure(fg=self.colors["red"])
        self.status.set(
            self._t("trustui.status.input").format(field=self._t(field_key))
        )

    def _required(self, variable: tk.StringVar, field_key: str) -> str | None:
        value = variable.get().strip()
        if not value:
            self._input_error(field_key)
            return None
        return value

    def _selected_ids(self, tree_name: str) -> list[str]:
        return [str(value) for value in self._trees[tree_name].selection()]

    def _enqueue_operation(self) -> None:
        task = self._required(self.operation_task, "trustui.field.task")
        if task is None:
            return
        workflow_id = self._choice_id(
            self.operation_workflow.get(),
            self._workflow_labels,
            WORKFLOW_IDS[0],
        )
        args: dict[str, Any] = {
            "operation_id": uuid.uuid4().hex,
            "workflow_id": workflow_id,
            "task_text": task,
            "working_directory": self.operation_directory.get().strip() or ".",
            "resource_key": self.operation_resource.get().strip() or "repo:main",
            "max_attempts": (
                3 if WORKFLOW_TEMPLATES[workflow_id]["automatic_retry"] else 1
            ),
            "timeout_seconds": 1800,
        }
        permission = self.operation_permission.get().strip()
        if permission:
            args["permission_decision_id"] = permission
        self._invoke("enqueue_workflow", args)

    def _cancel_operation(self) -> None:
        selected = self._selected_ids("operations")
        if len(selected) != 1:
            self._input_error("trustui.heading.operation")
            return
        self._invoke(
            "cancel_operation",
            {
                "operation_id": selected[0],
                "reason": self._t("trustui.cancel_reason"),
            },
        )

    def _reconcile_operations(self) -> None:
        self._invoke("reconcile_operations", {})

    def _render_release_gate(self, result: dict[str, Any]) -> None:
        self._last_release_result = dict(result)
        self._release_fingerprint = str(result.get("fingerprint") or "")
        operation = result.get("operation") or {}
        decision = result.get("human_decision") or {}
        if result.get("ready"):
            self.release_status.set(self._t("trustui.release.ready"))
            self.release_status_label.configure(fg=self.colors["green"])
            return
        self.release_status.set(
            self._t("trustui.release.summary").format(
                gate=self._localized_value("status", operation.get("status")),
                source=self._t(
                    "trustui.release.fresh"
                    if result.get("source_fresh")
                    else "trustui.release.stale"
                ),
                decision=self._t(
                    f"trustui.release.{decision.get('decision')}"
                    if decision.get("decision") in {"approve", "reject"}
                    else "trustui.release.pending"
                ),
            )
        )
        self.release_status_label.configure(fg=self.colors["amber"])

    def _request_release_gate(self) -> None:
        self._invoke("request_release_gate", {}, self._render_release_gate)

    def _check_release_gate(self) -> None:
        arguments = (
            {"fingerprint": self._release_fingerprint}
            if self._release_fingerprint
            else {}
        )
        self._invoke("release_gate_status", arguments, self._render_release_gate)

    def _decide_release_gate(self, decision: str) -> None:
        reason = self._required(self.release_reason, "trustui.field.release_reason")
        if reason is None:
            return
        if not self._release_fingerprint:
            self._input_error("trustui.action.request_release_gate")
            return
        self._invoke(
            "decide_release_gate",
            {
                "fingerprint": self._release_fingerprint,
                "decision": decision,
                "reason": reason,
            },
            self._render_release_gate,
        )

    def _save_schedule(self) -> None:
        task = self._required(self.schedule_task, "trustui.field.task")
        if task is None:
            return
        try:
            minutes = int(self.schedule_interval.get())
            if minutes < 1 or minutes > 44_640:
                raise ValueError
        except ValueError:
            self._input_error("trustui.field.interval")
            return
        interval = minutes * 60
        self._invoke(
            "save_workflow_schedule",
            {
                "schedule_id": uuid.uuid4().hex,
                "workflow_id": self._choice_id(
                    self.schedule_workflow.get(),
                    self._workflow_labels,
                    WORKFLOW_IDS[-1],
                ),
                "task_text": task,
                "working_directory": self.schedule_directory.get().strip() or ".",
                "resource_key": self.schedule_resource.get().strip() or "repo:release",
                "interval_seconds": interval,
                "next_run_epoch": time.time() + interval,
                "enabled": bool(self.schedule_enabled.get()),
            },
        )

    def _toggle_schedule(self, enabled: bool) -> None:
        selected = self._selected_ids("schedules")
        if len(selected) != 1:
            self._input_error("trustui.heading.schedule")
            return
        self._invoke(
            "set_workflow_schedule_enabled",
            {"schedule_id": selected[0], "enabled": enabled},
        )

    def _materialize_schedules(self) -> None:
        self._invoke("materialize_workflow_schedules", {"limit": 20})

    def _record_trust(self) -> None:
        task = self._required(self.trust_task, "trustui.field.task")
        statement = self._required(self.trust_statement, "trustui.field.statement")
        if task is None or statement is None:
            return
        self._invoke(
            "record_trust",
            {
                "record_id": uuid.uuid4().hex,
                "task_id": task,
                "stage": self._choice_id(
                    self.trust_stage.get(),
                    self._stage_labels,
                    GENERIC_TRUST_STAGES[0],
                ),
                "statement": statement,
                "artifact_paths": split_control_values(self.trust_artifacts.get()),
                "related_record_ids": self._selected_ids("trust"),
            },
        )

    def _record_disagreement(self) -> None:
        task = self._required(self.trust_task, "trustui.field.task")
        statement = self._required(self.trust_statement, "trustui.field.statement")
        records = self._selected_ids("trust")
        if task is None or statement is None:
            return
        if len(records) < 2:
            self._input_error("trustui.action.disagreement")
            return
        self._invoke(
            "record_trust_disagreement",
            {
                "task_id": task,
                "statement": statement,
                "evidence_record_ids": records,
            },
        )

    def _recheck_trust(self) -> None:
        statement = self._required(self.trust_statement, "trustui.field.statement")
        records = self._selected_ids("trust")
        if statement is None:
            return
        if len(records) != 1:
            self._input_error("trustui.action.recheck")
            return
        self._invoke(
            "recheck_trust_record",
            {"record_id": records[0], "statement": statement},
        )

    def _complete_trust(self) -> None:
        task = self._required(self.trust_task, "trustui.field.task")
        statement = self._required(self.trust_statement, "trustui.field.statement")
        records = self._selected_ids("trust")
        if task is None or statement is None:
            return
        if len(records) < 3:
            self._input_error("trustui.action.complete")
            return
        self._invoke(
            "complete_trust_timeline",
            {
                "task_id": task,
                "statement": statement,
                "evidence_record_ids": records,
            },
        )

    def _decide_permission(self) -> None:
        task = self._required(self.permission_task, "trustui.field.task")
        agent = self._required(self.permission_agent, "trustui.field.agent")
        action = self._required(self.permission_action, "trustui.field.action")
        resource = self._required(self.permission_resource, "trustui.field.resource")
        reason = self._required(self.permission_reason, "trustui.field.reason")
        if None in {task, agent, action, resource, reason}:
            return
        try:
            minutes = int(self.permission_expiry.get())
            if minutes < 1 or minutes > 1440:
                raise ValueError
        except ValueError:
            self._input_error("trustui.field.expires")
            return
        decision = self._choice_id(
            self.permission_decision.get(), self._permission_labels, "allow"
        )

        def accepted(result: dict[str, Any]) -> None:
            decision_id = str(result.get("decision_id") or "")
            self.worktree_permission.set(decision_id)
            self.worktree_task.set(str(result.get("task_id") or ""))
            self.worktree_agent.set(str(result.get("agent_id") or ""))

        self._invoke(
            "decide_permission",
            {
                "decision_id": uuid.uuid4().hex,
                "task_id": task,
                "agent_id": agent,
                "action": action,
                "resource_key": resource,
                "decision": decision,
                "reason": reason,
                "expires_epoch": time.time() + minutes * 60,
            },
            accepted,
        )

    def _create_worktree(self) -> None:
        task = self._required(self.worktree_task, "trustui.field.task")
        agent = self._required(self.worktree_agent, "trustui.field.agent")
        permission = self._required(
            self.worktree_permission, "trustui.field.permission"
        )
        if None in {task, agent, permission}:
            return
        binding = self.worktree_binding.get().strip() or uuid.uuid4().hex
        self.worktree_binding.set(binding)

        def created(result: dict[str, Any]) -> None:
            repository = Path(str(result.get("repository_root") or "")).resolve()
            if repository != self.project_root:
                self._input_error("trustui.field.directory")
                return
            self.operation_workflow.set(
                self._choice_label("implement-review", self._workflow_labels)
            )
            self.operation_directory.set(".")
            self.operation_permission.set(permission)
            self.operation_resource.set(repository_resource_key(self.project_root))

        self._invoke(
            "create_execution_worktree",
            {
                "binding_id": binding,
                "task_id": task,
                "agent_id": agent,
                "permission_decision_id": permission,
                "repository": self.worktree_repository.get().strip() or ".",
                "base_commit": self.worktree_base.get().strip() or "HEAD",
            },
            created,
        )

    def _seal_worktree(self) -> None:
        binding = self._selected_or_entered_binding()
        if binding is not None:
            self._invoke("seal_execution", {"binding_id": binding})

    def _verify_worktree(self) -> None:
        binding = self._selected_or_entered_binding()
        if binding is not None:
            self._invoke(
                "verify_execution_source",
                {"binding_id": binding},
                self._render_proof_result,
            )

    def _selected_or_entered_binding(self) -> str | None:
        selected = self._selected_ids("worktrees")
        binding = selected[0] if len(selected) == 1 else self.worktree_binding.get().strip()
        if not binding:
            self._input_error("trustui.field.binding")
            return None
        return binding

    def _brief_task(self) -> None:
        task = self._required(self.briefing_task, "trustui.field.task")
        if task is None:
            return
        args: dict[str, Any] = {
            "task_id": task,
            "applicability": split_control_values(
                self.briefing_applicability.get()
            ),
        }
        room = self.briefing_room.get().strip()
        if room:
            args["room_id"] = room
        self._invoke("brief_task", args)

    def _record_conflict(self) -> None:
        task = self._required(self.briefing_task, "trustui.field.task")
        summary = self._required(self.conflict_summary, "trustui.field.summary")
        selected = self._selected_ids("briefings")
        memory_ids = split_control_values(self.conflict_memory_ids.get())
        if task is None or summary is None:
            return
        if len(selected) != 1 or not memory_ids:
            self._input_error("trustui.action.record_conflict")
            return
        self._invoke(
            "record_decision_conflict",
            {
                "task_id": task,
                "briefing_id": selected[0],
                "memory_ids": memory_ids,
                "summary": summary,
                "severity": self._choice_id(
                    self.conflict_severity.get(),
                    self._severity_labels,
                    "medium",
                ),
            },
        )

    def _export_proof(self) -> None:
        task = self._required(self.proof_task, "trustui.field.task")
        if task is None:
            return
        output = self.proof_output.get().strip()
        if not output:
            try:
                output = proof_bundle_output_path(task)
            except ValueError:
                self._input_error("trustui.field.task")
                return
            self.proof_output.set(output)

        def exported(result: dict[str, Any]) -> None:
            self.proof_bundle.set(output)
            self._render_proof_result(result)

        self._invoke(
            "export_proof_bundle",
            {"task_id": task, "output_path": output},
            exported,
        )

    def _choose_proof(self) -> None:
        initial = self.project_root / ".peerbridge-artifacts" / "proof-bundles"
        initial.mkdir(parents=True, exist_ok=True)
        selected = filedialog.askdirectory(
            parent=self.frame.winfo_toplevel(),
            title=self._t("trustui.dialog.bundle"),
            initialdir=str(initial),
            mustexist=True,
        )
        if not selected:
            return
        resolved = Path(selected).resolve()
        try:
            relative = resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            self._input_error("trustui.field.bundle")
            return
        self.proof_bundle.set(relative)

    def _verify_proof(self) -> None:
        bundle = self._required(self.proof_bundle, "trustui.field.bundle")
        if bundle is not None:
            self._invoke(
                "verify_proof_bundle",
                {"bundle_path": bundle},
                self._render_proof_result,
            )

    def _render_proof_result(self, result: dict[str, Any]) -> None:
        rendered = redact_secrets(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        )
        self.proof_result_widget.configure(state="normal")
        self.proof_result_widget.delete("1.0", "end")
        self.proof_result_widget.insert("1.0", rendered)
        self.proof_result_widget.configure(state="disabled")

    def _permission_selected(self, _event: Any = None) -> None:
        selected = self._selected_ids("permissions")
        if len(selected) != 1:
            return
        record = self._records["permissions"].get(selected[0], {})
        self.worktree_permission.set(str(record.get("decision_id") or ""))
        self.worktree_task.set(str(record.get("task_id") or ""))
        self.worktree_agent.set(str(record.get("agent_id") or ""))

    def _worktree_selected(self, _event: Any = None) -> None:
        selected = self._selected_ids("worktrees")
        if len(selected) == 1:
            self.worktree_binding.set(selected[0])

    def _briefing_selected(self, _event: Any = None) -> None:
        selected = self._selected_ids("briefings")
        if len(selected) != 1:
            return
        record = self._records["briefings"].get(selected[0], {})
        self.briefing_task.set(str(record.get("task_id") or ""))
        bindings = record.get("memory_bindings")
        if not isinstance(bindings, list):
            try:
                bindings = json.loads(str(record.get("memory_bindings_json") or "[]"))
            except json.JSONDecodeError:
                bindings = []
        self.conflict_memory_ids.set(
            ", ".join(
                str(item.get("memory_id"))
                for item in bindings
                if isinstance(item, dict) and item.get("memory_id")
            )
        )

    def render(self, snapshot: Any, query: str = "") -> None:
        self._snapshot = snapshot
        self.refresh_verification_status()
        query = str(query or "").lower()
        self._replace(
            "operations",
            snapshot.operations,
            lambda row: str(row.get("operation_id")),
            lambda row: (
                row.get("operation_id"),
                self._localized_value("workflow", row.get("workflow_id")),
                self._localized_value("status", row.get("status")),
                f"{row.get('attempt_count', 0)}/{row.get('max_attempts', 0)}",
                row.get("updated_utc"),
            ),
            query,
        )
        self._replace(
            "schedules",
            snapshot.schedules,
            lambda row: str(row.get("schedule_id")),
            lambda row: (
                row.get("schedule_id"),
                self._localized_value("workflow", row.get("workflow_id")),
                self._yes_no(bool(row.get("enabled"))),
                self._epoch(row.get("next_run_epoch")),
                row.get("updated_utc"),
            ),
            query,
        )
        self._replace(
            "trust",
            snapshot.trust_records,
            lambda row: str(row.get("record_id")),
            lambda row: (
                self._localized_value("stage", row.get("stage")),
                row.get("task_id"),
                row.get("actor"),
                self._freshness(str(row.get("freshness") or "unavailable")),
                str(row.get("statement") or "")[:160],
                row.get("created_utc"),
            ),
            query,
        )
        self._replace(
            "permissions",
            snapshot.permission_decisions,
            lambda row: str(row.get("decision_id")),
            lambda row: (
                row.get("decision_id"),
                row.get("task_id"),
                row.get("agent_id"),
                row.get("action"),
                self._localized_value("status", row.get("decision")),
                self._epoch(row.get("expires_epoch")),
            ),
            query,
        )
        self._replace(
            "worktrees",
            snapshot.execution_bindings,
            lambda row: str(row.get("binding_id")),
            lambda row: (
                row.get("binding_id"),
                row.get("task_id"),
                row.get("agent_id"),
                self._localized_value("status", row.get("state")),
                str(row.get("final_commit_id") or row.get("base_commit_id") or "")[:12],
                str(row.get("final_diff_sha256") or row.get("base_diff_sha256") or "")[:12],
            ),
            query,
        )
        self._replace(
            "capabilities",
            snapshot.capabilities,
            lambda row: f"{row.get('capability_id')}:{row.get('registry_version')}",
            lambda row: (
                row.get("capability_id"),
                row.get("registry_version"),
                row.get("kind"),
                row.get("sensitivity"),
                self._yes_no(bool(row.get("enabled"))),
                str(row.get("capability_sha256") or "")[:12],
            ),
            query,
        )
        self._replace(
            "grants",
            snapshot.capability_grants,
            lambda row: str(row.get("grant_id")),
            lambda row: (
                row.get("grant_id"),
                f"{row.get('principal_type')}:{row.get('principal_id')}",
                f"{row.get('capability_id')}@{row.get('registry_version')}",
                self._localized_value("status", row.get("decision")),
                row.get("created_utc"),
            ),
            query,
        )
        self._replace(
            "briefings",
            snapshot.task_briefings,
            lambda row: str(row.get("briefing_id")),
            lambda row: (
                row.get("briefing_id"),
                row.get("task_id"),
                row.get("agent_id"),
                len(self._json_list(row.get("memory_bindings_json"))),
                row.get("created_utc"),
            ),
            query,
        )
        self._replace(
            "conflicts",
            snapshot.decision_conflicts,
            lambda row: str(row.get("finding_id")),
            lambda row: (
                row.get("finding_id"),
                row.get("task_id"),
                self._localized_value("severity", row.get("severity")),
                str(row.get("summary") or "")[:160],
                row.get("created_utc"),
            ),
            query,
        )

    def _replace(
        self,
        name: str,
        records: Iterable[dict[str, Any]],
        identity: Callable[[dict[str, Any]], str],
        values: Callable[[dict[str, Any]], tuple[Any, ...]],
        query: str,
    ) -> None:
        tree = self._trees[name]
        selected = set(tree.selection())
        tree.delete(*tree.get_children())
        self._records[name].clear()
        for record in records:
            if query and query not in json.dumps(record, ensure_ascii=False).lower():
                continue
            row_id = identity(record)
            if not row_id:
                continue
            tree.insert("", "end", iid=row_id, values=values(record))
            self._records[name][row_id] = dict(record)
        retained = [row_id for row_id in selected if row_id in self._records[name]]
        if retained:
            tree.selection_set(retained)

    def _show_detail(self, name: str) -> None:
        selected = self._selected_ids(name)
        if len(selected) != 1:
            return
        record = self._records[name].get(selected[0])
        if record is None:
            return
        window = tk.Toplevel(self.frame)
        window.title(self._t("trustui.detail.title"))
        window.geometry("760x520")
        window.configure(bg=self.colors["bg"])
        window.transient(self.frame.winfo_toplevel())
        text = tk.Text(
            window,
            wrap="none",
            bg=self.colors["black"],
            fg=self.colors["text"],
            font=("Cascadia Mono", 9),
            padx=10,
            pady=8,
        )
        scroll = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scroll.pack(side="right", fill="y", padx=(0, 10), pady=10)
        rendered = redact_secrets(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
        )
        text.insert("1.0", rendered)
        text.configure(state="disabled")

    def self_test(self) -> dict[str, bool]:
        return {
            "five_primary_tabs": len(self.notebook.tabs()) == 5,
            "operations": "operations" in self._trees,
            "schedules": "schedules" in self._trees,
            "trust_timeline": str(self.trust_tree.cget("selectmode")) == "extended",
            "permissions": "permissions" in self._trees,
            "worktrees": "worktrees" in self._trees,
            "capabilities": "capabilities" in self._trees,
            "decisions": {"briefings", "conflicts"}.issubset(self._trees),
            "proof_controls": all(
                button.cget("state") == "normal" for button in self._action_buttons
            ),
            "background_verification_controls": bool(
                self.verification_scan_button.cget("command")
                and self.verification_status_label.winfo_exists()
            ),
            "release_gate_controls": bool(
                self.release_status_label.winfo_exists()
                and self.release_reason.get() == ""
            ),
        }

    def _yes_no(self, value: bool) -> str:
        return self._t("trustui.yes" if value else "trustui.no")

    def _freshness(self, value: str) -> str:
        key = value if value in {"fresh", "stale", "invalid", "unavailable"} else "unavailable"
        return self._t(f"trustui.freshness.{key}")

    @staticmethod
    def _epoch(value: Any) -> str:
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime(
                "%Y-%m-%d %H:%MZ"
            )
        except (TypeError, ValueError, OSError):
            return "--"

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        try:
            decoded = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []


__all__ = [
    "GENERIC_TRUST_STAGES",
    "TrustWorkflowsPage",
    "proof_bundle_output_path",
    "split_control_values",
]
