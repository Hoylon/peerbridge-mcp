from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "pyproject.toml",
}
IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".peerbridge",
    ".pytest_cache",
    ".ruff_cache",
    ".tools",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "drafts",
    "htmlcov",
    "logs",
    "venv",
}
PRIVATE_PATTERNS = (
    re.compile(r"(?i)source_discovery_[A-Za-z0-9_-]+"),
    re.compile(r"(?i)[A-Z]:\\Users\\[^\\\r\n]+"),
)
SECRET = re.compile(
    r"(?i)(?:sk-|ghp_|github_pat_|Bearer\s+)[A-Za-z0-9_\-.]{16,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def iter_text_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        parts = path.relative_to(ROOT).parts
        if any(part in IGNORED_PARTS or part.endswith(".egg-info") for part in parts):
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    errors: list[str] = []
    missing = sorted(name for name in REQUIRED if not (ROOT / name).is_file())
    if missing:
        errors.append("missing required files: " + ", ".join(missing))

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = re.search(
        r'__version__\s*=\s*"([^"]+)"',
        (ROOT / "src" / "peerbridge_mcp" / "__init__.py").read_text(encoding="utf-8"),
    )
    if not package_version or project["project"]["version"] != package_version.group(1):
        errors.append("pyproject.toml and package __version__ differ")

    local_markers = tuple(
        marker.strip()
        for marker in os.environ.get("PEERBRIDGE_RELEASE_DENYLIST", "").split(",")
        if marker.strip()
    )
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        if any(pattern.search(text) for pattern in PRIVATE_PATTERNS):
            errors.append(f"private path or project marker in {relative}")
        if any(marker in text for marker in local_markers):
            errors.append(f"operator denylist match in {relative}")
        if SECRET.search(text):
            errors.append(f"credential-like value in {relative}")

    if errors:
        print("RELEASE_CHECK_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"RELEASE_CHECK_OK files={len(iter_text_files())} version={project['project']['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
