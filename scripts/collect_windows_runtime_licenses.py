"""Collect exact license texts for a PeerBridge Windows portable build."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Any


SCHEMA = "peerbridge.windows-runtime-licenses.v1"


class LicenseCollectionError(RuntimeError):
    """Raised when an exact redistributed license cannot be collected."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_license(
    source: Path,
    output_root: Path,
    target_name: str,
    component: str,
    source_label: str,
) -> dict[str, Any]:
    if not source.is_file():
        raise LicenseCollectionError(f"required license is missing: {source_label}")
    target = output_root / target_name
    if target.exists():
        raise LicenseCollectionError(f"duplicate license target: {target_name}")
    shutil.copyfile(source, target)
    return {
        "component": component,
        "path": target.name,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "source": source_label,
    }


def _distribution_licenses(
    distribution_name: str,
    component_name: str,
    output_root: Path,
) -> tuple[str, list[dict[str, Any]]]:
    try:
        distribution = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise LicenseCollectionError(
            f"required build distribution is missing: {distribution_name}"
        ) from exc
    candidates = sorted(
        (
            item
            for item in (distribution.files or ())
            if "license" in item.name.lower() or "copying" in item.name.lower()
        ),
        key=lambda item: item.as_posix(),
    )
    if not candidates:
        raise LicenseCollectionError(
            f"no license files found for distribution: {distribution_name}"
        )
    records = []
    for index, item in enumerate(candidates, start=1):
        source = Path(distribution.locate_file(item)).resolve()
        suffix = source.suffix or ".txt"
        target_name = (
            f"{component_name}-{distribution.version}-{index:02d}-"
            f"{source.stem}{suffix}"
        )
        records.append(
            _copy_license(
                source,
                output_root,
                target_name,
                component_name,
                f"distribution:{distribution_name}/{item.as_posix()}",
            )
        )
    return distribution.version, records


def collect(bundle_root: Path, output_root: Path) -> dict[str, Any]:
    bundle = bundle_root.resolve()
    destination = output_root.resolve()
    if not bundle.is_dir():
        raise LicenseCollectionError(f"portable bundle is missing: {bundle}")
    if destination.exists():
        raise LicenseCollectionError(
            f"runtime-license output is create-only and already exists: {destination}"
        )
    destination.mkdir(parents=True)

    internal = bundle / "_internal"
    python_dlls = sorted(internal.glob("python3*.dll"))
    if not python_dlls:
        raise LicenseCollectionError("portable bundle does not contain CPython")

    files: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    python_record = _copy_license(
        python_license,
        destination,
        f"Python-{platform.python_version()}-LICENSE.txt",
        "Python",
        "python-runtime/LICENSE.txt",
    )
    files.append(python_record)
    components.append(
        {
            "name": "Python",
            "version": platform.python_version(),
            "scope": "bundled-runtime",
            "licenses": [python_record["path"]],
        }
    )

    pyinstaller_version, pyinstaller_records = _distribution_licenses(
        "pyinstaller", "PyInstaller", destination
    )
    files.extend(pyinstaller_records)
    components.append(
        {
            "name": "PyInstaller",
            "version": pyinstaller_version,
            "scope": "bundled-bootloader",
            "licenses": [record["path"] for record in pyinstaller_records],
        }
    )

    cryptography_infos = sorted(internal.glob("cryptography-*.dist-info"))
    if len(cryptography_infos) != 1:
        raise LicenseCollectionError(
            "portable bundle must contain exactly one cryptography distribution"
        )
    cryptography_info = cryptography_infos[0]
    cryptography_version = cryptography_info.name.removeprefix("cryptography-").removesuffix(
        ".dist-info"
    )
    cryptography_sources = sorted((cryptography_info / "licenses").glob("*"))
    if not cryptography_sources:
        raise LicenseCollectionError("bundled cryptography licenses are missing")
    cryptography_records = []
    for index, source in enumerate(cryptography_sources, start=1):
        if not source.is_file():
            continue
        suffix = source.suffix or ".txt"
        record = _copy_license(
            source,
            destination,
            f"cryptography-{cryptography_version}-{index:02d}-{source.stem}{suffix}",
            "cryptography",
            f"bundle/_internal/{cryptography_info.name}/licenses/{source.name}",
        )
        cryptography_records.append(record)
        files.append(record)
    components.append(
        {
            "name": "cryptography",
            "version": cryptography_version,
            "scope": "bundled-runtime",
            "licenses": [record["path"] for record in cryptography_records],
        }
    )

    if list(internal.glob("_cffi_backend*.pyd")):
        cffi_version, cffi_records = _distribution_licenses(
            "cffi", "cffi", destination
        )
        files.extend(cffi_records)
        components.append(
            {
                "name": "cffi",
                "version": cffi_version,
                "scope": "bundled-runtime",
                "licenses": [record["path"] for record in cffi_records],
            }
        )

    if (internal / "tcl86t.dll").is_file() or (internal / "tk86t.dll").is_file():
        tcl_tk_source = internal / "_tk_data" / "license.terms"
        tcl_tk_record = _copy_license(
            tcl_tk_source,
            destination,
            "Tcl-Tk-8.6-license.terms",
            "Tcl-Tk",
            "bundle/_internal/_tk_data/license.terms",
        )
        files.append(tcl_tk_record)
        components.append(
            {
                "name": "Tcl-Tk",
                "version": "8.6",
                "scope": "bundled-runtime",
                "licenses": [tcl_tk_record["path"]],
            }
        )

    manifest = {
        "schema": SCHEMA,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "components": sorted(components, key=lambda row: row["name"].lower()),
        "files": sorted(files, key=lambda row: row["path"].lower()),
    }
    manifest_path = destination / "LICENSES_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "status": "PASS",
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "components": len(components),
        "license_files": len(files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = collect(args.bundle_root, args.output_root)
    except LicenseCollectionError as exc:
        print(f"WINDOWS_RUNTIME_LICENSE_FAIL {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
