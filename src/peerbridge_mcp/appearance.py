"""First-run desktop-surface choice for Pixel and Modern PeerBridge clients."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from .localization import (
    LocalizationError,
    default_preferences,
    load_preferences,
    save_preferences,
)


DESKTOP_SURFACES = ("pixel", "modern")


def appearance_preference_path(project_root: Path) -> Path:
    return project_root.resolve() / ".peerbridge" / "ui-preferences.json"


def saved_desktop_surface(project_root: Path) -> str | None:
    if not appearance_preference_path(project_root).is_file():
        return None
    preferences = load_preferences(project_root)
    surface = str(preferences.get("theme") or "")
    return surface if surface in DESKTOP_SURFACES else None


def save_desktop_surface(project_root: Path, surface: str) -> dict[str, Any]:
    if surface not in DESKTOP_SURFACES:
        raise LocalizationError("unsupported desktop surface")
    try:
        current = load_preferences(project_root)
    except LocalizationError:
        current = default_preferences()
    return save_preferences(
        project_root,
        locale=str(current["locale"]),
        tutorial_completed=bool(current["tutorial_completed"]),
        theme=surface,
    )


def _draw_pixel_preview(canvas: Any) -> None:
    canvas.configure(background="#101419", highlightthickness=0)
    canvas.create_rectangle(0, 0, 88, 230, fill="#171d24", outline="")
    canvas.create_text(12, 16, anchor="nw", text="PEERBRIDGE", fill="#5dd9e8", font=("Cascadia Mono", 10, "bold"))
    for index, label in enumerate(("01 CHAT", "02 WORK", "03 AUDIT", "04 CONNECT")):
        y = 48 + index * 30
        canvas.create_rectangle(10, y, 76, y + 21, fill="#202832" if index else "#5dd9e8", outline="#36414f")
        canvas.create_text(16, y + 11, anchor="w", text=label, fill="#080b0f" if index == 0 else "#e8edf2", font=("Cascadia Mono", 6, "bold"))
    canvas.create_rectangle(100, 14, 344, 46, fill="#171d24", outline="#36414f")
    canvas.create_text(112, 30, anchor="w", text="ROOM // AGENTS 4 // LIVE", fill="#ffc857", font=("Cascadia Mono", 7, "bold"))
    canvas.create_rectangle(100, 58, 344, 172, fill="#171d24", outline="#36414f")
    canvas.create_text(112, 70, anchor="nw", text="codex-main > reviewing diff\nclaude-code > tests passed\ngrok > searching sources\nkimi > waiting", fill="#e8edf2", font=("Cascadia Mono", 7))
    canvas.create_rectangle(100, 184, 344, 218, fill="#080b0f", outline="#36414f")
    canvas.create_text(112, 201, anchor="w", text="> send message to room", fill="#91a0ad", font=("Cascadia Mono", 7))


def _draw_modern_preview(canvas: Any) -> None:
    canvas.configure(background="#f6f7f9", highlightthickness=0)
    canvas.create_rectangle(0, 0, 78, 230, fill="#f1f3f6", outline="")
    canvas.create_text(11, 15, anchor="nw", text="PeerBridge", fill="#151922", font=("Segoe UI", 9, "bold"))
    for index, label in enumerate(("Chat", "Work", "Review", "Changes")):
        y = 46 + index * 27
        if index == 0:
            canvas.create_rectangle(8, y, 70, y + 20, fill="#ffffff", outline="#d8dde6")
        canvas.create_text(15, y + 10, anchor="w", text=label, fill="#151922", font=("Segoe UI", 6))
    canvas.create_text(94, 17, anchor="nw", text="Alpha 5.2 readiness", fill="#151922", font=("Segoe UI", 9, "bold"))
    canvas.create_oval(321, 18, 327, 24, fill="#24805c", outline="")
    canvas.create_rectangle(94, 45, 338, 183, fill="#ffffff", outline="#d8dde6")
    canvas.create_rectangle(190, 64, 325, 96, fill="#eef1f5", outline="")
    canvas.create_text(201, 80, anchor="w", text="Review the release diff", fill="#151922", font=("Segoe UI", 7))
    canvas.create_text(108, 111, anchor="nw", text="PeerBridge", fill="#151922", font=("Segoe UI", 7, "bold"))
    canvas.create_text(108, 132, anchor="nw", text="3 Agents responded\nTests and audit passed", fill="#667085", font=("Segoe UI", 7))
    canvas.create_rectangle(94, 194, 338, 220, fill="#ffffff", outline="#d8dde6")
    canvas.create_text(106, 207, anchor="w", text="Message the room...", fill="#667085", font=("Segoe UI", 7))


def choose_desktop_surface(project_root: Path) -> str | None:
    """Show a first-run, local-only chooser and persist the selected surface."""

    import tkinter as tk

    selected: list[str] = []
    root = tk.Tk()
    root.title("PeerBridge · Choose appearance / 選擇外觀")
    root.geometry("860x560")
    root.minsize(760, 520)
    root.configure(background="#f6f7f9")
    with contextlib.suppress(tk.TclError):
        icon = tk.PhotoImage(
            file=str(Path(__file__).with_name("release_support") / "peerbridge-icon.png")
        )
        root.iconphoto(True, icon)
        root._peerbridge_icon = icon  # type: ignore[attr-defined]

    title = tk.Label(
        root,
        text="Choose your PeerBridge workspace\n選擇你的 PeerBridge 工作介面",
        background="#f6f7f9",
        foreground="#151922",
        justify="left",
        font=("Segoe UI Variable Display", 20, "bold"),
    )
    title.pack(anchor="w", padx=34, pady=(28, 5))
    subtitle = tk.Label(
        root,
        text="You can change this later. 之後可以重新選擇。",
        background="#f6f7f9",
        foreground="#667085",
        font=("Segoe UI", 10),
    )
    subtitle.pack(anchor="w", padx=36, pady=(0, 22))

    selection = tk.StringVar(value="modern")
    cards = tk.Frame(root, background="#f6f7f9")
    cards.pack(fill="both", expand=True, padx=28)
    cards.grid_columnconfigure(0, weight=1, uniform="surface")
    cards.grid_columnconfigure(1, weight=1, uniform="surface")

    card_frames: dict[str, tk.Frame] = {}

    def refresh_cards() -> None:
        for key, frame in card_frames.items():
            frame.configure(
                highlightbackground="#315fc4" if selection.get() == key else "#d8dde6",
                highlightthickness=2 if selection.get() == key else 1,
            )

    def make_card(column: int, surface: str, heading: str, description: str) -> None:
        frame = tk.Frame(cards, background="#ffffff", highlightbackground="#d8dde6", highlightthickness=1)
        frame.grid(row=0, column=column, sticky="nsew", padx=8, pady=4)
        card_frames[surface] = frame
        canvas = tk.Canvas(frame, width=350, height=230, background="#ffffff", highlightthickness=0)
        canvas.pack(fill="x", padx=12, pady=(12, 9))
        (_draw_pixel_preview if surface == "pixel" else _draw_modern_preview)(canvas)
        tk.Radiobutton(
            frame,
            text=heading,
            variable=selection,
            value=surface,
            command=refresh_cards,
            background="#ffffff",
            activebackground="#ffffff",
            foreground="#151922",
            selectcolor="#ffffff",
            anchor="w",
            font=("Segoe UI", 12, "bold"),
        ).pack(fill="x", padx=14)
        tk.Label(
            frame,
            text=description,
            background="#ffffff",
            foreground="#667085",
            justify="left",
            wraplength=320,
            font=("Segoe UI", 9),
        ).pack(fill="x", padx=16, pady=(5, 14))
        frame.bind("<Button-1>", lambda _event, value=surface: (selection.set(value), refresh_cards()))
        canvas.bind("<Button-1>", lambda _event, value=surface: (selection.set(value), refresh_cards()))

    make_card(0, "pixel", "Pixel Control Room · 像素控制室", "Dense terminal-first monitoring with the original dark pixel style.\n保留原本深色像素風與高密度控制。")
    make_card(1, "modern", "Modern Workbench · 現代工作台", "Conversation-first workspace with model, permission, diff, and evidence controls.\n以對話為中心，整合模型、權限、變更及證據。")
    refresh_cards()

    actions = tk.Frame(root, background="#f6f7f9")
    actions.pack(fill="x", padx=36, pady=22)

    def confirm() -> None:
        surface = selection.get()
        save_desktop_surface(project_root, surface)
        selected.append(surface)
        root.destroy()

    tk.Button(
        actions,
        text="Continue / 繼續",
        command=confirm,
        background="#151922",
        foreground="#ffffff",
        activebackground="#2b3445",
        activeforeground="#ffffff",
        relief="flat",
        padx=24,
        pady=9,
        font=("Segoe UI", 10, "bold"),
    ).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return selected[0] if selected else None
