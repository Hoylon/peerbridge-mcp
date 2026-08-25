from __future__ import annotations

import struct
from pathlib import Path

import pytest

from peerbridge_mcp.appearance import (
    CHOOSER_GEOMETRY,
    CHOOSER_MAXIMUM_SIZE,
    CHOOSER_MINIMUM_SIZE,
    CHOOSER_COPY,
    _draw_modern_preview,
    _draw_pixel_preview,
    appearance_preference_path,
    chooser_locale_from_tag,
    preferred_chooser_locale,
    save_desktop_surface,
    saved_desktop_surface,
)
from peerbridge_mcp.localization import (
    LocalizationError,
    SUPPORTED_LOCALES,
    load_preferences,
)


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


def test_desktop_surface_selection_persists_first_run_locale(tmp_path: Path) -> None:
    saved = save_desktop_surface(tmp_path, "modern", locale="en")

    assert saved["theme"] == "modern"
    assert saved["locale"] == "en"
    assert load_preferences(tmp_path)["locale"] == "en"


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


@pytest.mark.parametrize("surface", ["pixel", "modern"])
def test_first_run_uses_real_1400_by_900_interface_capture(surface: str) -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "release_support"
        / f"peerbridge-{surface}-preview.png"
    )
    payload = path.read_bytes()

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", payload[16:24]) == (1400, 900)
    assert len(payload) > 50_000


def test_first_run_chooser_default_geometry_keeps_actions_visible() -> None:
    width, height = (int(value) for value in CHOOSER_GEOMETRY.split("x", 1))
    assert width >= CHOOSER_MINIMUM_SIZE[0]
    assert height >= CHOOSER_MINIMUM_SIZE[1]
    assert width <= CHOOSER_MAXIMUM_SIZE[0]
    assert height <= CHOOSER_MAXIMUM_SIZE[1]
    assert height >= 720


@pytest.mark.parametrize(
    ("language_tag", "expected"),
    [
        ("zh_TW", "zh-Hant"),
        ("zh-HK", "zh-Hant"),
        ("zh_CN", "zh-Hans"),
        ("zh-SG", "zh-Hans"),
        ("en_US", "en"),
        (None, "en"),
    ],
)
def test_first_run_locale_maps_system_language(
    language_tag: str | None, expected: str
) -> None:
    assert chooser_locale_from_tag(language_tag) == expected


def test_first_run_copy_is_complete_for_every_locale() -> None:
    expected_keys = set(CHOOSER_COPY["en"])

    assert set(CHOOSER_COPY) == set(SUPPORTED_LOCALES)
    assert all(set(copy) == expected_keys for copy in CHOOSER_COPY.values())
    assert all(all(value.strip() for value in copy.values()) for copy in CHOOSER_COPY.values())


def test_first_run_uses_detected_system_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "peerbridge_mcp.appearance.system_locale.getlocale",
        lambda: ("zh_HK", "UTF-8"),
    )

    assert preferred_chooser_locale() == "zh-Hant"
