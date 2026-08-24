from __future__ import annotations

from pathlib import Path

import pytest

from peerbridge_mcp.appearance import (
    CHOOSER_GEOMETRY,
    CHOOSER_MINIMUM_SIZE,
    _draw_modern_preview,
    _draw_pixel_preview,
    appearance_preference_path,
    save_desktop_surface,
    saved_desktop_surface,
)
from peerbridge_mcp.localization import LocalizationError, load_preferences


def test_desktop_surface_is_unselected_before_first_run(tmp_path: Path) -> None:
    assert appearance_preference_path(tmp_path) == (
        tmp_path / ".peerbridge" / "ui-preferences.json"
    )
    assert saved_desktop_surface(tmp_path) is None


@pytest.mark.parametrize("surface", ["pixel", "modern"])
def test_desktop_surface_selection_is_persisted(
    tmp_path: Path, surface: str
) -> None:
    saved = save_desktop_surface(tmp_path, surface)

    assert saved["theme"] == surface
    assert saved_desktop_surface(tmp_path) == surface
    assert load_preferences(tmp_path)["theme"] == surface


def test_desktop_surface_rejects_unknown_values(tmp_path: Path) -> None:
    with pytest.raises(LocalizationError, match="unsupported desktop surface"):
        save_desktop_surface(tmp_path, "downloaded-theme")


def test_both_first_run_previews_render_with_tk() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        pixel = tk.Canvas(root, width=350, height=230)
        modern = tk.Canvas(root, width=350, height=230)
        _draw_pixel_preview(pixel)
        _draw_modern_preview(modern)
        root.update_idletasks()
        assert pixel.find_all()
        assert modern.find_all()
    finally:
        root.destroy()


def test_first_run_chooser_default_geometry_keeps_actions_visible() -> None:
    width, height = (int(value) for value in CHOOSER_GEOMETRY.split("x", 1))
    assert width >= CHOOSER_MINIMUM_SIZE[0]
    assert height >= CHOOSER_MINIMUM_SIZE[1]
    assert height >= 680
