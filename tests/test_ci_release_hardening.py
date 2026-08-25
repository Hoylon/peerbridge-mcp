from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).parents[1]
LOCK_ROOT = PROJECT_ROOT / "requirements"
SHA256_LINE = re.compile(r"^    --hash=sha256:[0-9a-f]{64}(?: \\)?$")


def _locked_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    active_name: str | None = None
    active_hashes = 0
    for line in path.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("#"):
            continue
        if not line.startswith(" "):
            if active_name is not None:
                assert active_hashes > 0
            requirement = line.removesuffix(" \\")
            name, version = requirement.split("==", maxsplit=1)
            assert re.fullmatch(r"[a-z0-9-]+", name)
            assert name not in versions
            versions[name] = version
            active_name = name
            active_hashes = 0
            continue
        assert active_name is not None
        assert SHA256_LINE.fullmatch(line)
        active_hashes += 1
    assert active_name is not None and active_hashes > 0
    return versions


def test_ci_dependency_locks_are_hashed_and_cover_direct_dependencies() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = _locked_versions(LOCK_ROOT / "ci-dev.lock")
    windows = _locked_versions(LOCK_ROOT / "windows-build.lock")
    bootstrap = _locked_versions(LOCK_ROOT / "pip-bootstrap.lock")

    for extra, locked in (("dev", dev), ("windows", windows)):
        for raw in project["project"]["optional-dependencies"][extra]:
            requirement = Requirement(raw)
            assert locked[requirement.name.lower()] == str(requirement.specifier).removeprefix(
                "=="
            )
    assert bootstrap == {
        "pip": "26.1.2",
        "packaging": "26.3",
        "setuptools": "84.0.0",
        "wheel": "0.48.0",
    }
    assert set(dev) == {
        "build",
        "cffi",
        "colorama",
        "coverage",
        "cryptography",
        "iniconfig",
        "packaging",
        "pluggy",
        "pycparser",
        "pygments",
        "pyproject-hooks",
        "pytest",
        "pytest-cov",
        "setuptools",
        "wheel",
    }
    assert set(windows) == {
        "altgraph",
        "bottle",
        "cffi",
        "clr-loader",
        "cryptography",
        "packaging",
        "pefile",
        "proxy-tools",
        "pycparser",
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "pythonnet",
        "pywebview",
        "pywin32-ctypes",
        "setuptools",
        "typing-extensions",
        "wheel",
    }
    assert "--no-build-isolation" in (LOCK_ROOT / "windows-build.lock").read_text(
        encoding="ascii"
    )


def test_publication_credential_is_isolated_from_release_preparation() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    publish = workflow.split("  publish-release:\n", maxsplit=1)[1]
    preparation = workflow.split("  publish-release:\n", maxsplit=1)[0]

    assert workflow.count("contents: write") == 1
    assert "contents: write" not in preparation
    assert "contents: read" in preparation
    assert "actions/setup-python@" not in publish
    assert "actions/download-artifact@" in publish
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in publish
    assert "id-token: write" in publish
    assert "attestations: write" in publish
    assert "create-storage-record: false" in publish
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in publish
    assert "fetch-depth: 0" in publish
    assert publish.index("actions/checkout@") < publish.index("gh release create")
    assert 'gh release create "${GITHUB_REF_NAME}"' in publish
    assert workflow.count("persist-credentials: false") == workflow.count(
        "actions/checkout@"
    )


def test_ci_installs_only_hash_locked_external_python_dependencies() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    dependency_installs = [
        line.strip()
        for line in workflow.splitlines()
        if "python -m pip install" in line and " -r requirements/" in line
    ]

    assert len(dependency_installs) == 6
    assert all("--require-hashes" in line for line in dependency_installs)
    assert "pip install -e \".[" not in workflow
    assert 'pip install "pip==' not in workflow
    assert workflow.count("--no-deps --no-build-isolation -e .") == 3


def test_published_vm_acceptance_validates_static_provenance_before_execution() -> None:
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "release-vm-acceptance.yml"
    ).read_text(encoding="utf-8")
    static_job, execution_job = workflow.split("  clean-windows-vm:", maxsplit=1)

    assert "  static-provenance:" in static_job
    assert "persist-credentials: false" in static_job
    assert "verify_portable_provenance.py" in static_job
    assert "verify_windows_portable.ps1" not in static_job.split(
        "Stage the trusted verifier kit", maxsplit=1
    )[0]
    assert "actions/checkout@" not in execution_job
    assert "actions/download-artifact@" in execution_job
    assert "Remove-Item Env:GITHUB_TOKEN" in execution_job
    assert "Remove-Item Env:ACTIONS_RUNTIME_TOKEN" in execution_job
    assert "$beforeKit = Get-TreeDigest $kit" in execution_job
    assert "$afterKit = Get-TreeDigest $kit" in execution_job
    assert "$beforeKit -cne $afterKit" in execution_job
    assert "checkout_present = $false" in execution_job
