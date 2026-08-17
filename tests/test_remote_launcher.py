from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher lifecycle")

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launch_remote_control.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
    r"System32\WindowsPowerShell\v1.0\powershell.exe"
)
TASKKILL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / r"System32\taskkill.exe"
TEST_SENTINEL = "isolated-fixture-v1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if KERNEL32 is not None:
    KERNEL32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    KERNEL32.OpenProcess.restype = ctypes.c_void_p
    KERNEL32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    KERNEL32.GetExitCodeProcess.restype = ctypes.c_int
    KERNEL32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    KERNEL32.TerminateProcess.restype = ctypes.c_int
    KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
    KERNEL32.CloseHandle.restype = ctypes.c_int
    KERNEL32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    KERNEL32.CreateFileW.restype = ctypes.c_void_p


FAKE_BACKEND = r'''
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


if "--fixture-child" in sys.argv:
    while True:
        time.sleep(60)

parser = argparse.ArgumentParser()
parser.add_argument("--project-root")
parser.add_argument("--db")
parser.add_argument("--scope")
parser.add_argument("--host")
parser.add_argument("--port", type=int)
parser.add_argument("--public-origin")
parser.add_argument("--instance-id")
parser.add_argument("--evidence-run-id")
args = parser.parse_args()

proxy_credential = os.environ.get("PEERBRIDGE_REMOTE_PROXY_CREDENTIAL", "")
if len(proxy_credential) < 43:
    raise SystemExit("missing fixture proxy credential")
proxy_credential_sha256 = hashlib.sha256(proxy_credential.encode("utf-8")).hexdigest()

db_path = Path(args.db)
if not db_path.is_absolute():
    db_path = Path.cwd() / db_path
db_path.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(db_path)
db.execute("CREATE TABLE IF NOT EXISTS fixture_lifecycle (value TEXT NOT NULL)")
db.commit()

log_path = Path(os.environ["PB_FAKE_BACKEND_LOG"])
log_path.write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "pid": os.getpid(),
            "proxy_credential_present": True,
            "proxy_credential_sha256": proxy_credential_sha256,
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
child = subprocess.Popen(
    [sys.executable, __file__, "--fixture-child"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
evidence_path = Path(os.environ["PB_FAKE_BACKEND_EVIDENCE"])
temporary = evidence_path.with_suffix(".tmp")
temporary.write_text(
    json.dumps(
        {"wrapper": os.getppid(), "parent": os.getpid(), "child": child.pid},
        sort_keys=True,
    ),
    encoding="utf-8",
)
temporary.replace(evidence_path)

if os.environ.get("PB_FAKE_BACKEND_MODE") == "timeout":
    while True:
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        health = {
            "status": "ok",
            "instance_id": args.instance_id,
            "process_id": os.getpid(),
            "proxy_credential_sha256": proxy_credential_sha256,
        }
        if args.evidence_run_id is not None:
            health["evidence_run_id"] = args.evidence_run_id
        body = json.dumps(health, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_values: object) -> None:
        return


server = ThreadingHTTPServer((args.host, args.port), Handler)
server.daemon_threads = True
server.serve_forever()
'''


FAKE_TAILSCALE = r'''
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


args = sys.argv[1:]
log_path = Path(os.environ["PB_FAKE_TAILSCALE_LOG"])
with log_path.open("a", encoding="utf-8") as target:
    target.write(
        json.dumps(
            {"argv": args, "pid": os.getpid(), "time_ns": time.time_ns()},
            sort_keys=True,
        )
        + "\n"
    )
expected = os.environ["PB_FAKE_EXPECTED_TARGET"]
if any(value.lower() == "funnel" for value in args):
    print("Funnel is forbidden", file=sys.stderr)
    raise SystemExit(91)
if args == ["status", "--json"]:
    print(
        json.dumps(
            {
                "BackendState": "Running",
                "CertDomains": ["peerbridge-fixture.example.ts.net"],
                "Self": {
                    "DNSName": "peerbridge-fixture.example.ts.net.",
                    "Online": True,
                },
            },
            sort_keys=True,
        )
    )
    raise SystemExit(0)
if args == ["serve", "--bg", "--yes", expected]:
    print("fixture Serve configured")
    raise SystemExit(0)
if args == ["serve", "status", "--json"]:
    mode = os.environ.get("PB_FAKE_SERVE_STATUS", "valid")
    if mode == "invalid-json":
        print("not-json")
    elif mode == "wrong-target":
        print(
            json.dumps(
                {"Web": {"Note": expected, "Proxy": "http://127.0.0.1:1"}},
                sort_keys=True,
            )
        )
    elif mode == "funnel-enabled":
        print(
            json.dumps(
                {"Web": {"Proxy": expected}, "Funnel": {"Enabled": True}},
                sort_keys=True,
            )
        )
    else:
        print(json.dumps({"Web": {"Proxy": expected}}, sort_keys=True))
    raise SystemExit(0)
print("unexpected fake Tailscale arguments", file=sys.stderr)
raise SystemExit(92)
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _process_alive(pid: int) -> bool:
    process = KERNEL32.OpenProcess(0x1000, False, int(pid))
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not KERNEL32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        KERNEL32.CloseHandle(process)


def _wait_dead(pids: set[int], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_process_alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return not any(_process_alive(pid) for pid in pids)


def _taskkill_tree(pid: int) -> None:
    # Fixture descendants are already recorded individually, so the harness can
    # terminate each exact owned identity without a blocking taskkill/WMI walk.
    process = KERNEL32.OpenProcess(0x0001 | 0x100000, False, int(pid))
    if not process:
        return
    try:
        KERNEL32.TerminateProcess(process, 1)
    finally:
        KERNEL32.CloseHandle(process)


def _fixture_process_ids(root: Path) -> set[int]:
    # Every fixture-owned process writes its PID before the launcher can return.
    # Reading those create-only records is deterministic and avoids an expensive,
    # failure-prone full-machine WMI command-line scan.
    pids: set[int] = set()
    for evidence_path in root.glob("backend-*-pids.json"):
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(evidence, dict):
            continue
        pids.update(
            int(evidence[key])
            for key in ("wrapper", "parent", "child")
            if evidence.get(key)
        )
    for ownership_path in (
        root / ".peerbridge" / "remote-control.pid",
        root / ".peerbridge" / "remote-control-v2.pid",
    ):
        try:
            ownership = json.loads(ownership_path.read_text(encoding="ascii"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(ownership, dict):
            continue
        pids.update(
            int(ownership[key])
            for key in ("launcher_pid", "pid")
            if ownership.get(key)
        )
    return {pid for pid in pids if _process_alive(pid)}


def _assert_exclusive_file_access(path: Path) -> None:
    if not path.is_file():
        return
    deadline = time.monotonic() + 5.0
    while True:
        handle = KERNEL32.CreateFileW(
            str(path),
            0x80000000,
            0,
            None,
            3,
            0x80,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle and handle != invalid_handle:
            KERNEL32.CloseHandle(handle)
            return
        error = ctypes.get_last_error()
        if error != 32 or time.monotonic() >= deadline:
            pytest.fail(
                f"test fixture file still has an open handle ({error}): {path}"
            )
        time.sleep(0.05)


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _valid_serve_status(target: str) -> str:
    return json.dumps({"Web": {"Proxy": target}}, sort_keys=True)


@dataclass
class LaunchResult:
    completed: subprocess.CompletedProcess[str]
    tailscale_log: Path
    backend_log: Path
    backend_evidence: Path
    process_ids: frozenset[int]
    launcher_pid: int

    @property
    def output(self) -> str:
        return self.completed.stdout + self.completed.stderr

    @property
    def pids(self) -> set[int]:
        return set(self.process_ids)


@dataclass
class LauncherHarness:
    root: Path
    backend_script: Path
    tailscale_script: Path
    tracked_pids: set[int] = field(default_factory=set)
    run_number: int = 0

    @property
    def state(self) -> Path:
        return self.root / ".peerbridge"

    def _collect_owned_pids(self, backend_evidence: Path) -> set[int]:
        pids: set[int] = set()
        if backend_evidence.is_file():
            evidence = json.loads(backend_evidence.read_text(encoding="utf-8"))
            pids.update(
                int(evidence[key])
                for key in ("wrapper", "parent", "child")
                if evidence.get(key)
            )
        for ownership_path in (
            self.state / "remote-control.pid",
            self.state / "remote-control-v2.pid",
        ):
            if not ownership_path.is_file():
                continue
            try:
                ownership = json.loads(ownership_path.read_text(encoding="ascii"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(ownership, dict):
                continue
            pids.update(
                int(ownership[key])
                for key in ("launcher_pid", "pid")
                if ownership.get(key)
            )
        pids.update(_fixture_process_ids(self.root))
        return pids

    def run(
        self,
        *,
        port: int,
        scope: str = "launcher-test",
        backend_mode: str = "success",
        serve_status: str = "valid",
        evidence_run_id: str = "",
        fail_stop_pid: int = 0,
        test_mode: bool = True,
        sentinel: bool = True,
    ) -> LaunchResult:
        self.run_number += 1
        suffix = str(self.run_number)
        tailscale_log = self.root / f"tailscale-{suffix}.jsonl"
        backend_log = self.root / f"backend-{suffix}.jsonl"
        backend_evidence = self.root / f"backend-{suffix}-pids.json"
        target = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update(
            {
                "PB_FAKE_TAILSCALE_LOG": str(tailscale_log),
                "PB_FAKE_BACKEND_LOG": str(backend_log),
                "PB_FAKE_BACKEND_EVIDENCE": str(backend_evidence),
                "PB_FAKE_BACKEND_MODE": backend_mode,
                "PB_FAKE_SERVE_STATUS": serve_status,
                "PB_FAKE_EXPECTED_TARGET": target,
            }
        )
        if sentinel:
            environment["PEERBRIDGE_REMOTE_LAUNCHER_TESTING"] = TEST_SENTINEL
        else:
            environment.pop("PEERBRIDGE_REMOTE_LAUNCHER_TESTING", None)
        command = [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            "-Port",
            str(port),
            "-Scope",
            scope,
        ]
        if evidence_run_id:
            command.extend(["-EvidenceRunId", evidence_run_id])
        if test_mode:
            command.append("-TestMode")
        command.extend(
            [
                "-TestRoot",
                str(self.root),
                "-TestBackendExecutable",
                sys.executable,
                "-TestBackendScript",
                str(self.backend_script),
                "-TestTailscaleExecutable",
                sys.executable,
                "-TestTailscaleScript",
                str(self.tailscale_script),
                "-TestHealthAttempts",
                "8",
                "-TestHealthDelayMilliseconds",
                "50",
                "-TestHealthTimeoutSeconds",
                "1",
                "-TestExternalTimeoutSeconds",
                "5",
            ]
        )
        if fail_stop_pid:
            command.extend(["-TestFailStopProcessId", str(fail_stop_pid)])
        launcher_stdout = self.root / f"launcher-{suffix}.stdout.log"
        launcher_stderr = self.root / f"launcher-{suffix}.stderr.log"
        launcher_pid = 0
        launcher_timed_out = False
        with launcher_stdout.open("w", encoding="utf-8") as stdout_target, (
            launcher_stderr.open("w", encoding="utf-8")
        ) as stderr_target:
            with subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=CREATE_NO_WINDOW,
            ) as launcher:
                launcher_pid = launcher.pid
                try:
                    returncode = launcher.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    launcher_timed_out = True
                    _taskkill_tree(launcher.pid)
                    try:
                        returncode = launcher.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        launcher.kill()
                        returncode = launcher.wait(timeout=10)
        completed = subprocess.CompletedProcess(
            command,
            returncode,
            launcher_stdout.read_text(encoding="utf-8", errors="replace"),
            launcher_stderr.read_text(encoding="utf-8", errors="replace"),
        )
        process_ids = frozenset(self._collect_owned_pids(backend_evidence))
        result = LaunchResult(
            completed,
            tailscale_log,
            backend_log,
            backend_evidence,
            process_ids,
            launcher_pid,
        )
        self.tracked_pids.update(result.pids)
        if launcher_timed_out:
            self.stop_all()
            self.assert_all_dead()
            raise AssertionError(f"PowerShell launcher timed out after 40 seconds:\n{result.output}")
        return result

    def track(self, pid: int) -> None:
        self.tracked_pids.add(int(pid))

    def stop_all(self) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.tracked_pids.update(_fixture_process_ids(self.root))
            alive = {pid for pid in self.tracked_pids if _process_alive(pid)}
            if not alive:
                return
            for pid in sorted(alive):
                _taskkill_tree(pid)
            if _wait_dead(alive, timeout=5):
                continue

    def assert_all_dead(self) -> None:
        self.tracked_pids.update(_fixture_process_ids(self.root))
        assert _wait_dead(self.tracked_pids), (
            "stray launcher fixture processes: "
            f"{sorted(pid for pid in self.tracked_pids if _process_alive(pid))}"
        )
        assert _fixture_process_ids(self.root) == set()

    def assert_handles_closed(self) -> None:
        for path in self.root.rglob("*.sqlite3"):
            _assert_exclusive_file_access(path)
        for path in self.state.glob("remote-control*.log"):
            _assert_exclusive_file_access(path)


@pytest.fixture
def launcher_harness(tmp_path: Path) -> LauncherHarness:
    root = tmp_path / "isolated-launcher-root"
    root.mkdir()
    (root / ".peerbridge-launcher-test-root").write_text("fixture\n", encoding="ascii")
    backend_script = root / "fake_backend.py"
    tailscale_script = root / "fake_tailscale.py"
    backend_script.write_text(FAKE_BACKEND, encoding="utf-8")
    tailscale_script.write_text(FAKE_TAILSCALE, encoding="utf-8")
    harness = LauncherHarness(root, backend_script, tailscale_script)
    try:
        yield harness
    finally:
        harness.stop_all()
        harness.assert_all_dead()
        harness.assert_handles_closed()


def test_test_dependencies_require_both_mode_and_isolated_sentinel(
    launcher_harness: LauncherHarness,
) -> None:
    no_mode = launcher_harness.run(port=_free_port(), test_mode=False)
    assert no_mode.completed.returncode != 0
    assert "overrides require -TestMode" in no_mode.output
    assert _read_json_lines(no_mode.tailscale_log) == []

    no_sentinel = launcher_harness.run(port=_free_port(), sentinel=False)
    assert no_sentinel.completed.returncode != 0
    assert "isolated launcher-test environment sentinel" in no_sentinel.output
    assert _read_json_lines(no_sentinel.tailscale_log) == []
    launcher_harness.assert_all_dead()


def test_health_timeout_kills_and_confirms_owned_parent_and_child(
    launcher_harness: LauncherHarness,
) -> None:
    result = launcher_harness.run(port=_free_port(), backend_mode="timeout")
    assert result.completed.returncode != 0
    assert "did not become ready" in result.output
    evidence = json.loads(result.backend_evidence.read_text(encoding="utf-8"))
    assert {"wrapper", "parent", "child"} == set(evidence)
    assert _wait_dead(result.pids)
    assert not (launcher_harness.state / "remote-control.pid").exists()
    assert not (launcher_harness.state / "remote-control-serve.json").exists()
    calls = [row["argv"] for row in _read_json_lines(result.tailscale_log)]
    assert calls == [["status", "--json"]]
    launcher_harness.assert_all_dead()
    launcher_harness.assert_handles_closed()


def test_success_validates_serve_json_before_tailnet_only_state_write(
    launcher_harness: LauncherHarness,
) -> None:
    port = _free_port()
    target = f"http://127.0.0.1:{port}"
    result = launcher_harness.run(port=port)
    assert result.completed.returncode == 0, result.output
    calls = [row["argv"] for row in _read_json_lines(result.tailscale_log)]
    assert calls == [
        ["status", "--json"],
        ["serve", "--bg", "--yes", target],
        ["serve", "status", "--json"],
    ]
    assert all("funnel" not in [str(value).lower() for value in call] for call in calls)

    backend = _read_json_lines(result.backend_log)[0]
    backend_args = list(backend["argv"])
    assert backend_args[backend_args.index("--host") + 1] == "127.0.0.1"
    assert backend_args[backend_args.index("--public-origin") + 1] == (
        "https://peerbridge-fixture.example.ts.net"
    )
    assert "0.0.0.0" not in backend_args

    access_path = launcher_harness.state / "remote-control-access-url.txt"
    access_url = access_path.read_text(encoding="utf-8")
    prefix = "https://peerbridge-fixture.example.ts.net/#"
    assert access_url.startswith(prefix)
    proxy_credential = access_url[len(prefix) :]
    assert len(proxy_credential) == 64
    proxy_credential_sha256 = hashlib.sha256(
        proxy_credential.encode("utf-8")
    ).hexdigest()
    assert backend["proxy_credential_present"] is True
    assert backend["proxy_credential_sha256"] == proxy_credential_sha256
    assert proxy_credential not in result.output
    assert proxy_credential not in json.dumps(backend_args)
    acl_environment = os.environ.copy()
    acl_environment["PB_ACCESS_FILE"] = str(access_path)
    acl_result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Import-Module \"$env:SystemRoot\\System32\\WindowsPowerShell\\v1.0\\Modules\\"
            "Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1\"; "
            "(Get-Acl -LiteralPath $env:PB_ACCESS_FILE).Sddl",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=acl_environment,
        check=False,
    )
    assert acl_result.returncode == 0, acl_result.stderr
    assert "D:P" in acl_result.stdout

    state_path = launcher_harness.state / "remote-control-serve.json"
    state = json.loads(state_path.read_text(encoding="ascii"))
    expected_status = _valid_serve_status(target)
    assert state == {
        "public_origin": "https://peerbridge-fixture.example.ts.net",
        "local_backend": target,
        "scope": "launcher-test",
        "transport": "tailscale-serve",
        "tailnet_only": True,
        "funnel_enabled": False,
        "proxy_credential_sha256": proxy_credential_sha256,
        "validated_serve_status_sha256": hashlib.sha256(
            expected_status.encode("utf-8")
        ).hexdigest(),
        "configured_utc": state["configured_utc"],
    }
    assert state.get("configured_utc")
    evidence = json.loads(result.backend_evidence.read_text(encoding="utf-8"))
    assert all(_process_alive(int(evidence[key])) for key in ("parent", "child"))
    launcher_harness.stop_all()
    launcher_harness.assert_all_dead()
    launcher_harness.assert_handles_closed()


def test_evidence_run_id_binds_backend_lock_health_and_serve_state(
    launcher_harness: LauncherHarness,
) -> None:
    port = _free_port()
    run_id = "mobile-e2e-20260813-v1"
    launcher_harness.state.mkdir()
    legacy_stdout = launcher_harness.state / "remote-control.stdout.log"
    legacy_stderr = launcher_harness.state / "remote-control.stderr.log"
    legacy_stdout.write_bytes(b"historical stdout\n")
    legacy_stderr.write_bytes(b"historical stderr\n")
    result = launcher_harness.run(port=port, evidence_run_id=run_id)
    assert result.completed.returncode == 0, result.output

    assert legacy_stdout.read_bytes() == b"historical stdout\n"
    assert legacy_stderr.read_bytes() == b"historical stderr\n"
    assert (
        launcher_harness.state / f"remote-control-{run_id}.stdout.log"
    ).is_file()
    assert (
        launcher_harness.state / f"remote-control-{run_id}.stderr.log"
    ).is_file()

    backend_args = list(_read_json_lines(result.backend_log)[0]["argv"])
    assert backend_args[backend_args.index("--evidence-run-id") + 1] == run_id
    ownership = json.loads(
        (launcher_harness.state / "remote-control.pid").read_text(encoding="ascii")
    )
    assert ownership["evidence_run_id"] == run_id
    state = json.loads(
        (launcher_harness.state / "remote-control-serve.json").read_text(
            encoding="ascii"
        )
    )
    assert state["evidence_run_id"] == run_id


def test_matching_evidence_run_reuses_backend_but_mismatch_restarts_it(
    launcher_harness: LauncherHarness,
) -> None:
    port = _free_port()
    first = launcher_harness.run(port=port, evidence_run_id="run-a")
    assert first.completed.returncode == 0, first.output
    first_lock = json.loads(
        (launcher_harness.state / "remote-control.pid").read_text(encoding="ascii")
    )
    first_pid = int(first_lock["pid"])

    matching = launcher_harness.run(port=port, evidence_run_id="run-a")
    assert matching.completed.returncode == 0, matching.output
    assert "already running" in matching.output
    assert not matching.backend_log.exists()
    assert _process_alive(first_pid)

    replacement = launcher_harness.run(port=port, evidence_run_id="run-b")
    assert replacement.completed.returncode == 0, replacement.output
    replacement_lock = json.loads(
        (launcher_harness.state / "remote-control.pid").read_text(encoding="ascii")
    )
    assert replacement_lock["evidence_run_id"] == "run-b"
    assert int(replacement_lock["pid"]) != first_pid
    assert _wait_dead({first_pid})


def test_invalid_evidence_run_id_fails_before_tailscale_or_backend(
    launcher_harness: LauncherHarness,
) -> None:
    result = launcher_harness.run(
        port=_free_port(), evidence_run_id="invalid:windows-run"
    )
    assert result.completed.returncode != 0
    assert "EvidenceRunId must contain only" in result.output
    assert _read_json_lines(result.tailscale_log) == []
    assert not result.backend_evidence.exists()


@pytest.mark.parametrize(
    ("serve_status", "expected_error"),
    [
        ("invalid-json", "Serve status did not return valid JSON"),
        ("wrong-target", "Serve status does not bind the expected PeerBridge backend"),
        ("funnel-enabled", "Serve status reports a Funnel-enabled route"),
    ],
)
def test_invalid_serve_status_never_writes_state_and_cleans_backend(
    launcher_harness: LauncherHarness,
    serve_status: str,
    expected_error: str,
) -> None:
    result = launcher_harness.run(port=_free_port(), serve_status=serve_status)
    assert result.completed.returncode != 0
    assert expected_error in result.output
    assert expected_error in result.completed.stderr
    assert not (launcher_harness.state / "remote-control-serve.json").exists()
    assert not (launcher_harness.state / "remote-control.pid").exists()
    assert [row["argv"] for row in _read_json_lines(result.tailscale_log)][-1] == [
        "serve",
        "status",
        "--json",
    ]
    assert _wait_dead(result.pids)
    launcher_harness.assert_all_dead()
    launcher_harness.assert_handles_closed()


def test_stale_live_pid_identity_is_not_killed_and_lock_is_replaced(
    launcher_harness: LauncherHarness,
) -> None:
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    ) as sleeper:
        launcher_harness.track(sleeper.pid)
        launcher_harness.state.mkdir()
        lock_path = launcher_harness.state / "remote-control.pid"
        lock_path.write_text(
            json.dumps(
                {
                    "pid": sleeper.pid,
                    "start_time_utc_ticks": 1,
                    "port": _free_port(),
                    "scope": "stale",
                    "instance_id": "stale-instance",
                }
            ),
            encoding="ascii",
        )
        try:
            result = launcher_harness.run(port=_free_port())
            assert result.completed.returncode == 0, result.output
            current = json.loads(lock_path.read_text(encoding="ascii"))
            assert current["pid"] in result.pids
            assert current["pid"] != sleeper.pid
            assert _process_alive(sleeper.pid)
        finally:
            launcher_harness.stop_all()
            sleeper.wait(timeout=10)
    launcher_harness.assert_all_dead()
    launcher_harness.assert_handles_closed()


def test_legacy_bare_pid_lock_is_preserved_and_v2_ownership_is_used(
    launcher_harness: LauncherHarness,
) -> None:
    launcher_harness.state.mkdir()
    legacy_path = launcher_harness.state / "remote-control.pid"
    legacy_bytes = b"11968\r\n"
    legacy_path.write_bytes(legacy_bytes)

    result = launcher_harness.run(port=_free_port())
    assert result.completed.returncode == 0, result.output
    assert legacy_path.read_bytes() == legacy_bytes

    v2_path = launcher_harness.state / "remote-control-v2.pid"
    ownership = json.loads(v2_path.read_text(encoding="ascii"))
    assert ownership["pid"] in result.pids
    assert ownership["launcher_pid"] in result.pids
    launcher_harness.stop_all()
    launcher_harness.assert_all_dead()
    launcher_harness.assert_handles_closed()


def test_failed_owned_stop_preserves_exact_ownership_and_starts_nothing_new(
    launcher_harness: LauncherHarness,
) -> None:
    first = launcher_harness.run(port=_free_port(), scope="first-scope")
    assert first.completed.returncode == 0, first.output
    lock_path = launcher_harness.state / "remote-control.pid"
    original_lock = lock_path.read_bytes()
    ownership = json.loads(original_lock)
    owned_pid = int(ownership["pid"])
    owned_launcher_pid = int(ownership.get("launcher_pid") or owned_pid)
    assert _process_alive(owned_pid)
    assert _process_alive(owned_launcher_pid)

    second = launcher_harness.run(
        port=_free_port(),
        scope="replacement-scope",
        fail_stop_pid=owned_launcher_pid,
    )
    assert second.completed.returncode != 0
    assert "could not be stopped; ownership was retained" in second.output
    assert lock_path.read_bytes() == original_lock
    assert _process_alive(owned_pid)
    assert _process_alive(owned_launcher_pid)
    assert not second.backend_evidence.exists()
    assert [row["argv"] for row in _read_json_lines(second.tailscale_log)] == [
        ["status", "--json"]
    ]
    launcher_harness.stop_all()
    launcher_harness.assert_all_dead()
    launcher_harness.assert_handles_closed()
