"""Package cryptography without recording the build machine's install origin."""

from __future__ import annotations

from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


def _portable_metadata() -> list[tuple[str, str]]:
    package = distribution("cryptography")
    collected: list[tuple[str, str]] = []
    for relative in package.files or ():
        parts = relative.parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        if relative.name.casefold() == "direct_url.json":
            continue
        source = Path(package.locate_file(relative))
        if source.is_file():
            collected.append((str(source), str(Path(*parts[:-1]))))
    return collected


datas = _portable_metadata()
hiddenimports = (
    collect_submodules("cryptography.hazmat.backends")
    + collect_submodules("cryptography.hazmat.bindings.openssl")
    + ["_cffi_backend"]
)
