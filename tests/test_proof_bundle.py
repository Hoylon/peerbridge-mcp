from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from peerbridge_mcp.bridge import Bridge
from peerbridge_mcp.proof_bundle import (
    ProofBundleError,
    create_proof_bundle,
    verify_proof_bundle,
)
from peerbridge_mcp.trust_timeline import TrustTimeline


def _completed_timeline(root: Path) -> tuple[Bridge, TrustTimeline]:
    (root / "evidence" / "result.txt").parent.mkdir(parents=True)
    (root / "evidence" / "result.txt").write_text(
        "focused tests passed\n", encoding="utf-8"
    )
    bridge = Bridge(
        root,
        root / ".peerbridge" / "proof.sqlite3",
        "human-operator",
        "proof-test",
        session_id="proof-export",
    )
    timeline = TrustTimeline(bridge)
    records = []
    previous: list[str] = []
    for stage, statement in (
        ("test", "Focused tests passed against this source."),
        ("proof", "The bounded proof was captured from this source."),
        ("review", "A reviewer inspected this exact source."),
        ("decision", "The human approved this exact reviewed source."),
    ):
        record = timeline.record(
            task_id="task-one",
            stage=stage,
            statement=statement,
            artifact_paths=["evidence/result.txt"],
            related_record_ids=previous,
            record_id=f"task-one:{stage}",
        )
        records.append(record)
        previous = [record["record_id"]]
    timeline.record_completion(
        task_id="task-one",
        statement="The human accepted completion on the exact source.",
        evidence_record_ids=[record["record_id"] for record in records],
    )
    return bridge, timeline


def test_proof_bundle_is_create_only_portable_and_independently_verified(
    tmp_path: Path,
) -> None:
    bridge, _timeline = _completed_timeline(tmp_path)
    output = tmp_path / "exports" / "proof-one"
    result = create_proof_bundle(bridge, task_id="task-one", output_path=output)
    assert result["status"] == "CAPTURED"
    assert result["evidence_file_count"] == 1
    assert result["trust_record_count"] == 5
    assert result["trusted_external_verifier_required"] is True
    assert result["origin_authenticated"] is False
    assert verify_proof_bundle(output)["valid"] is True
    assert not (output / "verify_proof_bundle.py").exists()

    encoded = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            output / "proof-bundle.json",
            output / "PROOF_BUNDLE.md",
            output / "MANIFEST.json",
        )
    )
    assert str(tmp_path) not in encoded
    assert "payload_json" not in encoded
    assert '"terminal_history":' not in encoded
    proof = json.loads((output / "proof-bundle.json").read_text(encoding="utf-8"))
    assert proof["safety"]["raw_event_payloads_included"] is False
    assert proof["safety"]["terminal_history_included"] is False
    assert proof["safety"]["origin_authenticated"] is False
    assert proof["safety"]["verification_scope"] == "structural_consistency_only"
    assert all(
        not Path(item["project_path"]).is_absolute()
        for item in proof["evidence_files"]
    )

    portable = tmp_path / "relocated" / "bundle"
    portable.parent.mkdir()
    shutil.copytree(output, portable)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "peerbridge_mcp.proof_bundle",
            "verify",
            str(portable),
        ],
        cwd=portable,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout)["valid"] is True

    with pytest.raises(ProofBundleError, match="create-only"):
        create_proof_bundle(bridge, task_id="task-one", output_path=output)


def test_proof_bundle_verifier_detects_manifest_and_evidence_tampering(
    tmp_path: Path,
) -> None:
    bridge, _timeline = _completed_timeline(tmp_path)
    output = tmp_path / "proof"
    create_proof_bundle(bridge, task_id="task-one", output_path=output)
    (output / "evidence" / "evidence" / "result.txt").write_text(
        "tampered\n", encoding="utf-8"
    )
    invalid = verify_proof_bundle(output)
    assert invalid["valid"] is False
    assert "differs from manifest" in invalid["errors"][0]

    second = tmp_path / "proof-two"
    create_proof_bundle(bridge, task_id="task-one", output_path=second)
    manifest_path = second / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["safety"]["create_only"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_proof_bundle(second)["valid"] is False


def test_proof_bundle_verifier_rejects_oversized_unlisted_file(tmp_path: Path) -> None:
    bridge, _timeline = _completed_timeline(tmp_path)
    output = tmp_path / "proof"
    create_proof_bundle(bridge, task_id="task-one", output_path=output)
    oversized = output / "oversized.bin"
    with oversized.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)

    invalid = verify_proof_bundle(output)

    assert invalid["valid"] is False
    assert "exceeds the byte limit" in invalid["errors"][0]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_proof_bundle_verifier_rejects_windows_junctions(tmp_path: Path) -> None:
    bridge, _timeline = _completed_timeline(tmp_path)
    output = tmp_path / "proof"
    create_proof_bundle(bridge, task_id="task-one", output_path=output)
    external = tmp_path / "external"
    external.mkdir()
    (external / "outside.txt").write_text("outside\n", encoding="utf-8")
    junction = output / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("Windows did not permit a temporary directory junction")
    try:
        invalid = verify_proof_bundle(output)
        assert invalid["valid"] is False
        assert "reparse point" in invalid["errors"][0]
    finally:
        junction.rmdir()


def test_proof_bundle_rejects_secrets_absolute_paths_and_stale_sources(
    tmp_path: Path,
) -> None:
    bridge, timeline = _completed_timeline(tmp_path)
    source = tmp_path / "evidence" / "result.txt"
    credential_fixture = "sk-" + "example-" + "1234567890abcdef"
    source.write_text(f"api_key={credential_fixture}\n", encoding="utf-8")
    with pytest.raises(ProofBundleError, match="stale trust evidence"):
        create_proof_bundle(bridge, task_id="task-one", output_path=tmp_path / "stale")

    secret_root = tmp_path / "secret-case"
    secret_bridge, _secret_timeline = _completed_timeline(secret_root)
    secret_source = secret_root / "evidence" / "result.txt"
    secret_source.write_text(f"api_key={credential_fixture}\n", encoding="utf-8")
    # Rebuild a task after the unsafe evidence exists so freshness itself still passes.
    unsafe_bridge = Bridge(
        secret_root,
        secret_root / ".peerbridge" / "unsafe.sqlite3",
        "human-operator",
        "unsafe-proof",
        session_id="unsafe-proof-session",
    )
    unsafe_timeline = TrustTimeline(unsafe_bridge)
    records = []
    for stage in ("test", "proof", "review", "decision"):
        records.append(
            unsafe_timeline.record(
                task_id="unsafe",
                stage=stage,
                statement=f"Safe {stage} statement.",
                artifact_paths=["evidence/result.txt"],
                record_id=f"unsafe:{stage}",
            )
        )
    unsafe_timeline.record_completion(
        task_id="unsafe",
        statement="Unsafe evidence must not export.",
        evidence_record_ids=[item["record_id"] for item in records],
    )
    with pytest.raises(ProofBundleError, match="credential-like"):
        create_proof_bundle(
            unsafe_bridge,
            task_id="unsafe",
            output_path=secret_root / "unsafe-export",
        )
    assert not (secret_root / "unsafe-export").exists()

    absolute_root = tmp_path / "absolute-case"
    absolute_bridge, absolute_timeline = _completed_timeline(absolute_root)
    private_path = "C:" + r"\Users\Alice\project\source.txt"
    absolute_timeline.record(
        task_id="task-one",
        stage="claim",
        statement=f"A private source was observed at {private_path}.",
        record_id="absolute-claim",
    )
    with pytest.raises(ProofBundleError, match="absolute path"):
        create_proof_bundle(
            absolute_bridge,
            task_id="task-one",
            output_path=absolute_root / "absolute-export",
        )
