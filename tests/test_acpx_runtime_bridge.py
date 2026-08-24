from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("permission_tier", "expected_mode", "edit_outcome", "execute_outcome"),
    (
        ("review", "approve-reads", "reject_once", "reject_once"),
        ("edit", "approve-reads", "allow_once", "reject_once"),
        ("full-development", "approve-all", "allow_once", "allow_once"),
    ),
)
def test_acpx_runtime_bridge_maps_peerbridge_permission_tiers(
    tmp_path: Path,
    permission_tier: str,
    expected_mode: str,
    edit_outcome: str,
    execute_outcome: str,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    bridge = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "acpx_runtime_bridge.mjs"
    )
    runtime = tmp_path / "fake-permission-runtime.mjs"
    runtime.write_text(
        """
export function createAgentRegistry() {
  return { list() { return ["grok-build"]; } };
}
export function createRuntimeStore() {
  return {
    async load() {
      return { agentCapabilities: { promptCapabilities: {} } };
    },
  };
}
export function createAcpRuntime(options) {
  return {
    async ensureSession() {
      return { backend: "acp", runtimeSessionName: "permission-session" };
    },
    async getCapabilities() { return { controls: ["prompt"] }; },
    async getStatus() {
      const edit = await options.onPermissionRequest({ inferredKind: "edit" });
      const execute = await options.onPermissionRequest({ inferredKind: "execute" });
      return {
        models: {
          currentModelId: `${options.permissionMode}:${edit.outcome}:${execute.outcome}`,
          availableModelIds: [],
        },
      };
    },
    async close() {},
  };
}
""".strip(),
        encoding="utf-8",
    )
    request = {
        "operation": "ensure",
        "runtimeModulePath": str(runtime),
        "stateDir": str(tmp_path / "state"),
        "cwd": str(tmp_path),
        "agent": "grok-build",
        "sessionKey": f"peerbridge-{permission_tier}",
        "permissionTier": permission_tier,
    }

    result = subprocess.run(
        (node, str(bridge)),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["status"]["currentModelId"] == (
        f"{expected_mode}:{edit_outcome}:{execute_outcome}"
    )
    assert events[0]["permissionBoundary"] == {
        "review": "read-only",
        "edit": "scoped-edit",
        "full-development": "session-trusted",
    }[permission_tier]


def test_acpx_runtime_bridge_submits_native_image_without_echoing_bytes(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    bridge = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "acpx_runtime_bridge.mjs"
    )
    runtime = tmp_path / "fake-acpx-runtime.mjs"
    runtime.write_text(
        """
export function createAgentRegistry() {
  return { list() { return ["grok-build"]; } };
}
export function createRuntimeStore() {
  return {
    async load() {
      return {
        agentCapabilities: {
          promptCapabilities: { image: true, audio: false, embeddedContext: true },
        },
      };
    },
  };
}
export function createAcpRuntime() {
  return {
    async ensureSession() {
      return { backend: "acp", runtimeSessionName: "fake-session" };
    },
    async getCapabilities() { return { controls: ["prompt", "cancel"] }; },
    async getStatus() {
      return { models: { currentModelId: "grok-test", availableModelIds: ["grok-test"] } };
    },
    startTurn({ attachments }) {
      async function* events() {
        yield { type: "text_delta", stream: "answer", text: `SEEN:${attachments.length}` };
        yield { type: "status", breakdown: { inputTokens: 4, outputTokens: 2, totalTokens: 6 } };
        yield { type: "tool_call", title: "Read image", status: "completed", kind: "read" };
      }
      return {
        events: events(),
        result: Promise.resolve({ status: "completed", stopReason: "end_turn" }),
      };
    },
    async close() {},
  };
}
""".strip(),
        encoding="utf-8",
    )
    image_data = "cGVlcmJyaWRnZS1pbWFnZS1ieXRlcw=="
    request = {
        "operation": "turn",
        "runtimeModulePath": str(runtime),
        "stateDir": str(tmp_path / "state"),
        "cwd": str(tmp_path),
        "agent": "grok-build",
        "sessionKey": "peerbridge-test",
        "permissionTier": "review",
        "requestId": "request-test",
        "text": "Inspect the image.",
        "attachments": [{"mediaType": "image/png", "data": image_data}],
    }

    result = subprocess.run(
        (node, str(bridge)),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in events] == [
        "session",
        "transport",
        "text_delta",
        "status",
        "tool_call",
        "done",
    ]
    assert events[0]["promptCapabilities"] == {
        "image": True,
        "audio": False,
        "embeddedContext": True,
    }
    assert events[0]["permissionTier"] == "review"
    assert events[1] == {
        "type": "transport",
        "status": "native_acp_content_submitted",
        "attachmentCount": 1,
    }
    assert events[2]["text"] == "SEEN:1"
    assert events[3]["usage"] == {
        "inputTokens": 4,
        "outputTokens": 2,
        "totalTokens": 6,
    }
    assert events[4] == {
        "type": "tool_call",
        "title": "Read image",
        "status": "completed",
        "kind": "read",
    }
    assert image_data not in result.stdout


def test_acpx_runtime_bridge_rejects_image_before_turn_when_agent_does_not_advertise_it(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    bridge = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "acpx_runtime_bridge.mjs"
    )
    runtime = tmp_path / "fake-no-image-acpx-runtime.mjs"
    runtime.write_text(
        """
export function createAgentRegistry() {
  return { list() { return ["grok-build"]; } };
}
export function createRuntimeStore() {
  return {
    async load() {
      return {
        agentCapabilities: {
          promptCapabilities: { image: false, audio: false, embeddedContext: true },
        },
      };
    },
  };
}
export function createAcpRuntime() {
  return {
    async ensureSession() {
      return { backend: "acp", runtimeSessionName: "fake-session" };
    },
    async getCapabilities() { return { controls: ["prompt"] }; },
    async getStatus() { return { models: { currentModelId: "grok-test" } }; },
    startTurn() { throw new Error("startTurn must not run"); },
    async close() {},
  };
}
""".strip(),
        encoding="utf-8",
    )
    request = {
        "operation": "turn",
        "runtimeModulePath": str(runtime),
        "stateDir": str(tmp_path / "state"),
        "cwd": str(tmp_path),
        "agent": "grok-build",
        "sessionKey": "peerbridge-no-image",
        "permissionTier": "review",
        "requestId": "request-no-image",
        "text": "Inspect the image.",
        "attachments": [{"mediaType": "image/png", "data": "cGVlcmJyaWRnZQ=="}],
    }

    result = subprocess.run(
        (node, str(bridge)),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["promptCapabilities"]["image"] is False
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "ACP_IMAGE_INPUT_UNSUPPORTED"
    assert not any(event["type"] == "transport" for event in events)


def test_acpx_runtime_bridge_submits_native_audio_when_agent_advertises_it(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    bridge = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "acpx_runtime_bridge.mjs"
    )
    runtime = tmp_path / "fake-audio-acpx-runtime.mjs"
    runtime.write_text(
        """
export function createAgentRegistry() {
  return { list() { return ["grok-build"]; } };
}
export function createRuntimeStore() {
  return {
    async load() {
      return {
        agentCapabilities: {
          promptCapabilities: { image: false, audio: true, embeddedContext: true },
        },
      };
    },
  };
}
export function createAcpRuntime() {
  return {
    async ensureSession() {
      return { backend: "acp", runtimeSessionName: "fake-audio-session" };
    },
    async getCapabilities() { return { controls: ["prompt", "cancel"] }; },
    async getStatus() { return { models: { currentModelId: "grok-audio-test" } }; },
    startTurn({ attachments }) {
      if (attachments.length !== 1 || attachments[0].mediaType !== "audio/wav") {
        throw new Error("native audio block was not preserved");
      }
      async function* events() {
        yield { type: "text_delta", stream: "answer", text: "AUDIO_SEEN" };
      }
      return {
        events: events(),
        result: Promise.resolve({ status: "completed", stopReason: "end_turn" }),
      };
    },
    async close() {},
  };
}
""".strip(),
        encoding="utf-8",
    )
    audio_data = "UklGRggAAABXQVZFZGF0YQ=="
    request = {
        "operation": "turn",
        "runtimeModulePath": str(runtime),
        "stateDir": str(tmp_path / "state"),
        "cwd": str(tmp_path),
        "agent": "grok-build",
        "sessionKey": "peerbridge-audio",
        "permissionTier": "review",
        "requestId": "request-audio",
        "text": "Transcribe the audio.",
        "attachments": [{"mediaType": "audio/wav", "data": audio_data}],
    }

    result = subprocess.run(
        (node, str(bridge)),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["promptCapabilities"]["audio"] is True
    assert events[1] == {
        "type": "transport",
        "status": "native_acp_content_submitted",
        "attachmentCount": 1,
    }
    assert events[2]["text"] == "AUDIO_SEEN"
    assert audio_data not in result.stdout


def test_acpx_runtime_bridge_rejects_audio_before_turn_when_agent_does_not_advertise_it(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    bridge = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "acpx_runtime_bridge.mjs"
    )
    runtime = tmp_path / "fake-no-audio-acpx-runtime.mjs"
    runtime.write_text(
        """
export function createAgentRegistry() {
  return { list() { return ["grok-build"]; } };
}
export function createRuntimeStore() {
  return {
    async load() {
      return {
        agentCapabilities: {
          promptCapabilities: { image: true, audio: false, embeddedContext: true },
        },
      };
    },
  };
}
export function createAcpRuntime() {
  return {
    async ensureSession() {
      return { backend: "acp", runtimeSessionName: "fake-no-audio-session" };
    },
    async getCapabilities() { return { controls: ["prompt"] }; },
    async getStatus() { return { models: { currentModelId: "grok-test" } }; },
    startTurn() { throw new Error("startTurn must not run"); },
    async close() {},
  };
}
""".strip(),
        encoding="utf-8",
    )
    request = {
        "operation": "turn",
        "runtimeModulePath": str(runtime),
        "stateDir": str(tmp_path / "state"),
        "cwd": str(tmp_path),
        "agent": "grok-build",
        "sessionKey": "peerbridge-no-audio",
        "permissionTier": "review",
        "requestId": "request-no-audio",
        "text": "Transcribe the audio.",
        "attachments": [
            {"mediaType": "audio/wav", "data": "UklGRggAAABXQVZFZGF0YQ=="}
        ],
    }

    result = subprocess.run(
        (node, str(bridge)),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["promptCapabilities"]["audio"] is False
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "ACP_AUDIO_INPUT_UNSUPPORTED"
    assert not any(event["type"] == "transport" for event in events)


def test_acpx_runtime_bridge_rejects_noncanonical_attachment_base64(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    bridge = (
        Path(__file__).parents[1]
        / "src"
        / "peerbridge_mcp"
        / "acpx_runtime_bridge.mjs"
    )
    runtime = tmp_path / "fake-base64-acpx-runtime.mjs"
    runtime.write_text(
        """
export function createAgentRegistry() {
  return { list() { return ["grok-build"]; } };
}
export function createRuntimeStore() {
  return {
    async load() {
      return {
        agentCapabilities: {
          promptCapabilities: { image: true, audio: true, embeddedContext: true },
        },
      };
    },
  };
}
export function createAcpRuntime() {
  return {
    async ensureSession() {
      return { backend: "acp", runtimeSessionName: "fake-base64-session" };
    },
    async getCapabilities() { return { controls: ["prompt"] }; },
    async getStatus() { return { models: { currentModelId: "grok-test" } }; },
    startTurn() { throw new Error("startTurn must not run"); },
    async close() {},
  };
}
""".strip(),
        encoding="utf-8",
    )
    request = {
        "operation": "turn",
        "runtimeModulePath": str(runtime),
        "stateDir": str(tmp_path / "state"),
        "cwd": str(tmp_path),
        "agent": "grok-build",
        "sessionKey": "peerbridge-bad-base64",
        "permissionTier": "review",
        "requestId": "request-bad-base64",
        "text": "Inspect the attachment.",
        "attachments": [{"mediaType": "audio/wav", "data": "UklGRg"}],
    }

    result = subprocess.run(
        (node, str(bridge)),
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "PEERBRIDGE_ACP_RUNTIME_FAILED"
    assert "canonical base64" in events[-1]["message"]
    assert not any(event["type"] == "transport" for event in events)
