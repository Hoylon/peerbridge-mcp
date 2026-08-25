"""First-run desktop-surface choice for Pixel and Modern PeerBridge clients."""

from __future__ import annotations

import contextlib
import locale as system_locale
from pathlib import Path
from typing import Any

from .localization import (
    LOCALE_LABELS,
    LocalizationError,
    SUPPORTED_LOCALES,
    default_preferences,
    load_preferences,
    save_preferences,
)


DESKTOP_SURFACES = ("pixel", "modern")
CHOOSER_GEOMETRY = "1040x780"
CHOOSER_MINIMUM_SIZE = (960, 720)
CHOOSER_MAXIMUM_SIZE = (1100, 820)

CHOOSER_COPY: dict[str, dict[str, str]] = {
    "zh-Hant": {
        "window_title": "PeerBridge · 選擇工作介面",
        "language": "語言",
        "title": "選擇你的 PeerBridge 工作介面",
        "subtitle": "首次只需選擇一次，之後可在設定中更改。",
        "pixel_title": "像素控制室",
        "pixel_description": "深色像素風、高密度監控與完整控制面板。",
        "modern_title": "現代對話式介面",
        "modern_description": "以對話為中心，整合模型、權限、程式碼變更與證據。",
        "continue": "繼續",
    },
    "zh-Hans": {
        "window_title": "PeerBridge · 选择工作界面",
        "language": "语言",
        "title": "选择你的 PeerBridge 工作界面",
        "subtitle": "首次只需选择一次，之后可在设置中更改。",
        "pixel_title": "像素控制室",
        "pixel_description": "深色像素风、高密度监控与完整控制面板。",
        "modern_title": "现代对话式界面",
        "modern_description": "以对话为中心，整合模型、权限、代码变更与证据。",
        "continue": "继续",
    },
    "en": {
        "window_title": "PeerBridge · Choose workspace",
        "language": "Language",
        "title": "Choose your PeerBridge workspace",
        "subtitle": "Choose once now. You can change this later in Settings.",
        "pixel_title": "Pixel Control Room",
        "pixel_description": "Dark pixel styling with dense monitoring and complete controls.",
        "modern_title": "Modern Conversation",
        "modern_description": "Conversation-first work with models, permissions, code changes, and evidence.",
        "continue": "Continue",
    },
}


def chooser_locale_from_tag(language_tag: str | None) -> str:
    normalized = str(language_tag or "").strip().replace("_", "-").lower()
    if normalized.startswith(("zh-hant", "zh-tw", "zh-hk", "zh-mo")):
        return "zh-Hant"
    if normalized.startswith(("zh-hans", "zh-cn", "zh-sg")):
        return "zh-Hans"
    return "en"


def preferred_chooser_locale() -> str:
    try:
        language_tag = system_locale.getlocale()[0]
    except (ValueError, TypeError):
        language_tag = None
    return chooser_locale_from_tag(language_tag)


def _chooser_font(locale: str, size: int, weight: str = "normal") -> tuple[str, int, str]:
    family = {
        "zh-Hant": "Microsoft JhengHei UI",
        "zh-Hans": "Microsoft YaHei UI",
        "en": "Segoe UI Variable Text",
    }.get(locale, "Segoe UI")
    return (family, size, weight)


def appearance_preference_path(project_root: Path) -> Path:
    return project_root.resolve() / ".peerbridge" / "ui-preferences.json"


def saved_desktop_surface(project_root: Path) -> str | None:
    if not appearance_preference_path(project_root).is_file():
        return None
    preferences = load_preferences(project_root)
    surface = str(preferences.get("theme") or "")
    return surface if surface in DESKTOP_SURFACES else None


def save_desktop_surface(
    project_root: Path, surface: str, *, locale: str | None = None
) -> dict[str, Any]:
    if surface not in DESKTOP_SURFACES:
        raise LocalizationError("unsupported desktop surface")
    try:
        current = load_preferences(project_root)
    except LocalizationError:
        current = default_preferences()
    return save_preferences(
        project_root,
        locale=str(locale or current["locale"]),
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
    root.geometry(CHOOSER_GEOMETRY)
    root.minsize(*CHOOSER_MINIMUM_SIZE)
    root.maxsize(*CHOOSER_MAXIMUM_SIZE)
    root.configure(background="#f6f7f9")
    with contextlib.suppress(tk.TclError):
        icon = tk.PhotoImage(
            file=str(Path(__file__).with_name("release_support") / "peerbridge-icon.png")
        )
        root.iconphoto(True, icon)
        root._peerbridge_icon = icon  # type: ignore[attr-defined]

    locale_selection = tk.StringVar(value=preferred_chooser_locale())
    language_row = tk.Frame(root, background="#f6f7f9")
    language_row.pack(fill="x", padx=34, pady=(22, 4))
    language_label = tk.Label(
        language_row,
        background="#f6f7f9",
        foreground="#667085",
    )
    language_label.pack(side="left", padx=(0, 12))
    language_buttons: dict[str, tk.Radiobutton] = {}
    for locale in SUPPORTED_LOCALES:
        button = tk.Radiobutton(
            language_row,
            text=LOCALE_LABELS[locale],
            variable=locale_selection,
            value=locale,
            indicatoron=False,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=5,
            cursor="hand2",
        )
        button.pack(side="left", padx=(0, 5))
        language_buttons[locale] = button

    title = tk.Label(
        root,
        background="#f6f7f9",
        foreground="#151922",
        justify="left",
    )
    title.pack(anchor="w", padx=34, pady=(14, 5))
    subtitle = tk.Label(
        root,
        background="#f6f7f9",
        foreground="#667085",
    )
    subtitle.pack(anchor="w", padx=36, pady=(0, 22))

    selection = tk.StringVar(value="modern")
    cards = tk.Frame(root, background="#f6f7f9")
    cards.pack(fill="both", expand=True, padx=28)
    cards.grid_columnconfigure(0, weight=1, uniform="surface")
    cards.grid_columnconfigure(1, weight=1, uniform="surface")

    card_frames: dict[str, tk.Frame] = {}
    card_headings: dict[str, tk.Radiobutton] = {}
    card_descriptions: dict[str, tk.Label] = {}
    preview_images: list[tk.PhotoImage] = []

    def refresh_cards() -> None:
        for key, frame in card_frames.items():
            frame.configure(
                highlightbackground="#315fc4" if selection.get() == key else "#d8dde6",
                highlightthickness=2 if selection.get() == key else 1,
            )

    def make_card(column: int, surface: str) -> None:
        frame = tk.Frame(cards, background="#ffffff", highlightbackground="#d8dde6", highlightthickness=1)
        frame.grid(row=0, column=column, sticky="nsew", padx=8, pady=4)
        card_frames[surface] = frame
        canvas = tk.Canvas(frame, width=467, height=300, background="#ffffff", highlightthickness=0)
        canvas.pack(fill="x", padx=12, pady=(12, 9))
        preview_path = Path(__file__).with_name("release_support") / f"peerbridge-{surface}-preview.png"
        try:
            source_image = tk.PhotoImage(file=str(preview_path))
            preview_image = source_image.subsample(3, 3)
            preview_images.extend((source_image, preview_image))
            canvas.configure(background="#101419" if surface == "pixel" else "#ffffff")
            canvas.create_image(234, 150, anchor="center", image=preview_image)
        except tk.TclError:
            (_draw_pixel_preview if surface == "pixel" else _draw_modern_preview)(canvas)
        heading = tk.Radiobutton(
            frame,
            variable=selection,
            value=surface,
            command=refresh_cards,
            background="#ffffff",
            activebackground="#ffffff",
            foreground="#151922",
            selectcolor="#ffffff",
            anchor="w",
        )
        heading.pack(fill="x", padx=14)
        description = tk.Label(
            frame,
            background="#ffffff",
            foreground="#667085",
            justify="left",
            anchor="w",
            wraplength=440,
        )
        description.pack(fill="x", padx=16, pady=(5, 14))
        card_headings[surface] = heading
        card_descriptions[surface] = description
        frame.bind("<Button-1>", lambda _event, value=surface: (selection.set(value), refresh_cards()))
        canvas.bind("<Button-1>", lambda _event, value=surface: (selection.set(value), refresh_cards()))

    make_card(0, "pixel")
    make_card(1, "modern")
    refresh_cards()

    actions = tk.Frame(root, background="#f6f7f9")
    actions.pack(fill="x", padx=36, pady=22)

    def confirm() -> None:
        surface = selection.get()
        save_desktop_surface(
            project_root, surface, locale=locale_selection.get()
        )
        selected.append(surface)
        root.destroy()

    continue_button = tk.Button(
        actions,
        command=confirm,
        background="#151922",
        foreground="#ffffff",
        activebackground="#2b3445",
        activeforeground="#ffffff",
        relief="flat",
        padx=24,
        pady=9,
    )
    continue_button.pack(side="right")

    def refresh_language(*_args: object) -> None:
        locale = locale_selection.get()
        copy = CHOOSER_COPY[locale]
        root.title(copy["window_title"])
        language_label.configure(
            text=copy["language"], font=_chooser_font(locale, 10, "normal")
        )
        title.configure(text=copy["title"], font=_chooser_font(locale, 20, "bold"))
        subtitle.configure(
            text=copy["subtitle"], font=_chooser_font(locale, 10, "normal")
        )
        card_headings["pixel"].configure(
            text=copy["pixel_title"], font=_chooser_font(locale, 12, "bold")
        )
        card_headings["modern"].configure(
            text=copy["modern_title"], font=_chooser_font(locale, 12, "bold")
        )
        card_descriptions["pixel"].configure(
            text=copy["pixel_description"], font=_chooser_font(locale, 10, "normal")
        )
        card_descriptions["modern"].configure(
            text=copy["modern_description"], font=_chooser_font(locale, 10, "normal")
        )
        continue_button.configure(
            text=copy["continue"], font=_chooser_font(locale, 10, "bold")
        )
        for button_locale, button in language_buttons.items():
            active = button_locale == locale
            button.configure(
                background="#151922" if active else "#eef1f5",
                foreground="#ffffff" if active else "#344054",
                selectcolor="#151922" if active else "#eef1f5",
                activebackground="#2b3445" if active else "#e4e7ec",
                activeforeground="#ffffff" if active else "#151922",
                font=_chooser_font(button_locale, 9, "bold" if active else "normal"),
            )

    locale_selection.trace_add("write", refresh_language)
    refresh_language()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return selected[0] if selected else None
