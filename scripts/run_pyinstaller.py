from __future__ import annotations

import os
import platform
import runpy
import sys
import tomllib
import faulthandler
from pathlib import Path


ISOLATED_CHILD_ENV = "PEERBRIDGE_PYINSTALLER_ISOLATED_CHILD"


def _prime_windows_platform_snapshot() -> None:
    """Keep PyInstaller startup independent of the optional Windows WMI provider."""
    if sys.platform != "win32":
        return

    version = sys.getwindowsversion()
    release = (
        "11"
        if version.major == 10 and version.build >= 22000
        else "10"
        if version.major == 10
        else str(version.major)
    )
    version_text = f"{version.major}.{version.minor}.{version.build}"
    service_pack = getattr(version, "service_pack", "") or ""
    machine = os.environ.get("PROCESSOR_ARCHITECTURE") or (
        "AMD64" if sys.maxsize > 2**32 else "x86"
    )
    node = os.environ.get("COMPUTERNAME", "")

    platform.win32_ver = lambda *args, **kwargs: (  # type: ignore[assignment]
        release,
        version_text,
        service_pack,
        "",
    )
    uname_snapshot = platform.uname_result(
        "Windows",
        node,
        release,
        version_text,
        machine,
    )
    uname_snapshot.__dict__["processor"] = machine
    platform._uname_cache = uname_snapshot  # type: ignore[attr-defined]
    platform.processor = lambda: machine  # type: ignore[assignment]


def _enable_isolated_call_trace() -> None:
    """Expose the exact PyInstaller isolated call when diagnosing a build stall."""
    if os.environ.get("PEERBRIDGE_PYINSTALLER_ISOLATED_TRACE") != "1":
        return

    from PyInstaller.isolated import _parent

    original_call = _parent.Python.call

    def traced_call(self, function, *args, **kwargs):
        details = repr((args, kwargs))
        print(
            f"PEERBRIDGE_PYINSTALLER_ISOLATED_CALL "
            f"{function.__module__}.{function.__name__} {details[:500]}",
            file=sys.stderr,
            flush=True,
        )
        result = original_call(self, function, *args, **kwargs)
        print(
            f"PEERBRIDGE_PYINSTALLER_ISOLATED_DONE "
            f"{function.__module__}.{function.__name__}",
            file=sys.stderr,
            flush=True,
        )
        return result

    _parent.Python.call = traced_call


def _run_isolated_child_if_requested() -> bool:
    """Prime platform metadata before entering PyInstaller's isolated worker."""
    if os.environ.get(ISOLATED_CHILD_ENV) != "1":
        return False

    _prime_windows_platform_snapshot()
    from PyInstaller.isolated import _child

    runpy.run_path(str(Path(_child.__file__).resolve()), run_name="__main__")
    return True


def _install_isolated_child_wrapper() -> None:
    """Ensure every PyInstaller analysis child avoids the optional WMI provider."""
    from PyInstaller.isolated import _parent

    os.environ[ISOLATED_CHILD_ENV] = "1"
    _parent.CHILD_PY = Path(__file__).resolve()


def main() -> int:
    if _run_isolated_child_if_requested():
        return 0

    if len(sys.argv) == 3 and sys.argv[1] == "--peerbridge-project-version":
        project = tomllib.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(project["project"]["version"])
        return 0

    _prime_windows_platform_snapshot()
    trace_seconds = os.environ.get("PEERBRIDGE_PYINSTALLER_TRACE_SECONDS", "")
    if trace_seconds.isdigit() and int(trace_seconds) > 0:
        faulthandler.dump_traceback_later(int(trace_seconds), exit=True)
    try:
        from PyInstaller.__main__ import run
    except ImportError as exc:
        raise SystemExit(
            'PyInstaller is unavailable. Install the Windows dependency: '
            'python -m pip install -e ".[windows]"'
        ) from exc
    _install_isolated_child_wrapper()
    _enable_isolated_call_trace()
    run(sys.argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
