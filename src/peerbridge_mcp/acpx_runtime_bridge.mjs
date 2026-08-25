import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const MAX_REQUEST_BYTES = 32 * 1024 * 1024;
const MAX_ATTACHMENTS = 8;

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function fail(message, code = "PEERBRIDGE_ACP_RUNTIME_FAILED", detailCode) {
  emit({
    type: "error",
    message: String(message || "ACP runtime failed"),
    code,
    ...(detailCode ? { detailCode: String(detailCode) } : {}),
    retryable: false,
  });
  process.exitCode = 1;
}

function requireString(value, label, maximum = 4096) {
  if (typeof value !== "string" || !value.trim() || value.length > maximum || value.includes("\0")) {
    throw new Error(`${label} is invalid`);
  }
  return value;
}

function sanitizeBreakdown(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const result = {};
  for (const key of [
    "inputTokens",
    "outputTokens",
    "cachedReadTokens",
    "cachedWriteTokens",
    "thoughtTokens",
    "totalTokens",
  ]) {
    if (Number.isFinite(value[key]) && value[key] >= 0) result[key] = value[key];
  }
  return Object.keys(result).length ? result : undefined;
}

function sanitizeCost(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const result = {};
  if (Number.isFinite(value.amount) && value.amount >= 0) result.amount = value.amount;
  if (typeof value.currency === "string" && /^[A-Za-z]{3}$/.test(value.currency)) {
    result.currency = value.currency.toUpperCase();
  }
  return Object.keys(result).length ? result : undefined;
}

function sanitizeStatus(status) {
  const models = status?.models;
  const currentModelId = typeof models?.currentModelId === "string" ? models.currentModelId : undefined;
  const availableModelIds = Array.isArray(models?.availableModelIds)
    ? models.availableModelIds.filter((item) => typeof item === "string").slice(0, 128)
    : [];
  const usage = status?.usage;
  const availableCommands = Array.isArray(status?.availableCommands)
    ? status.availableCommands
        .filter((item) => item && typeof item.name === "string")
        .slice(0, 128)
        .map((item) => ({
          name: item.name,
          ...(typeof item.description === "string" ? { description: item.description.slice(0, 500) } : {}),
          ...(typeof item.hasInput === "boolean" ? { hasInput: item.hasInput } : {}),
        }))
    : [];
  return {
    ...(currentModelId ? { currentModelId } : {}),
    availableModelIds,
    ...(usage?.cumulative ? { cumulativeUsage: sanitizeBreakdown(usage.cumulative) } : {}),
    ...(usage?.cost ? { cost: sanitizeCost(usage.cost) } : {}),
    availableCommands,
  };
}

function sanitizePromptCapabilities(value) {
  const capabilities = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    image: capabilities.image === true,
    audio: capabilities.audio === true,
    embeddedContext: capabilities.embeddedContext === true,
  };
}

async function permissionDecision(permissionTier, approvalMode, request, approvalEndpoint, approvalToken) {
  const kind = typeof request?.inferredKind === "string" ? request.inferredKind : "other";
  if (permissionTier === "full-development") return { outcome: "allow_once" };
  if (approvalMode === "agent-delegated" && new Set(["read", "search", "edit", "fetch", "think"]).has(kind)) {
    return { outcome: "allow_once" };
  }
  if (typeof approvalEndpoint !== "string" || typeof approvalToken !== "string") {
    return { outcome: "reject_once" };
  }
  const endpoint = new URL(approvalEndpoint);
  if (endpoint.protocol !== "http:" || endpoint.hostname !== "127.0.0.1" || endpoint.pathname !== "/approval") {
    return { outcome: "reject_once" };
  }
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: ["Bearer", approvalToken].join(" "),
        "Content-Type": "application/json",
      },
      body: JSON.stringify(request),
      signal: AbortSignal.timeout(600000),
    });
    if (!response.ok) return { outcome: "reject_once" };
    const payload = await response.json();
    return new Set(["allow_once", "allow_always", "reject_once"]).has(payload?.outcome)
      ? { outcome: payload.outcome }
      : { outcome: "reject_once" };
  } catch {
    return { outcome: "reject_once" };
  }
}

function parseAttachments(value) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > MAX_ATTACHMENTS) {
    throw new Error("ACP attachment list is invalid");
  }
  return value.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("ACP attachment is invalid");
    }
    const mediaType = requireString(item.mediaType, "ACP attachment media type", 100);
    const kind = mediaType.startsWith("image/")
      ? "image"
      : mediaType.startsWith("audio/")
        ? "audio"
        : "";
    if (!kind) {
      throw new Error("ACP bridge accepts only image or audio content blocks");
    }
    const data = requireString(item.data, "ACP attachment data", MAX_REQUEST_BYTES);
    if (data.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)) {
      throw new Error("ACP attachment data is not canonical base64");
    }
    const decoded = Buffer.from(data, "base64");
    if (!decoded.length || decoded.toString("base64") !== data) {
      throw new Error("ACP attachment data is not canonical base64");
    }
    return { mediaType, data, kind };
  });
}

async function readRequest() {
  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    total += chunk.length;
    if (total > MAX_REQUEST_BYTES) throw new Error("ACP bridge request is too large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

let runtime;
let handle;
try {
  const input = await readRequest();
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("ACP bridge request must be an object");
  }
  const operation = requireString(input.operation, "ACP bridge operation", 20);
  if (!new Set(["ensure", "turn"]).has(operation)) {
    throw new Error("ACP bridge operation is unsupported");
  }
  const runtimeModulePath = path.resolve(requireString(input.runtimeModulePath, "ACPX runtime module path"));
  const stateDir = path.resolve(requireString(input.stateDir, "ACPX state directory"));
  const cwd = path.resolve(requireString(input.cwd, "ACP working directory"));
  const agent = requireString(input.agent, "ACP Agent id", 100);
  const sessionKey = requireString(input.sessionKey, "ACP session key", 200);
  const permissionTier = requireString(input.permissionTier, "ACP permission tier", 32);
  if (!new Set(["observe", "review", "edit", "full-development"]).has(permissionTier)) {
    throw new Error("ACP permission tier is unsupported");
  }
  const approvalMode = input.approvalMode === undefined
    ? (permissionTier === "full-development" ? "full-access" : permissionTier === "edit" ? "agent-delegated" : "approval-required")
    : requireString(input.approvalMode, "ACP approval mode", 32);
  if (!new Set(["approval-required", "agent-delegated", "full-access"]).has(approvalMode)) {
    throw new Error("ACP approval mode is unsupported");
  }
  const approvalEndpoint = typeof input.approvalEndpoint === "string" ? input.approvalEndpoint : undefined;
  const approvalToken = typeof input.approvalToken === "string" ? input.approvalToken : undefined;
  const timeoutMs = Number.isInteger(input.timeoutMs) && input.timeoutMs >= 1000 && input.timeoutMs <= 600000
    ? input.timeoutMs
    : 180000;
  if (!fs.statSync(runtimeModulePath).isFile() || !fs.statSync(cwd).isDirectory()) {
    throw new Error("ACP runtime module or working directory is unavailable");
  }
  fs.mkdirSync(stateDir, { recursive: true });

  const acpx = await import(pathToFileURL(runtimeModulePath).href);
  const registry = acpx.createAgentRegistry();
  if (!registry.list().includes(agent)) throw new Error("requested ACP Agent is not registered");
  const sessionStore = acpx.createRuntimeStore({ stateDir });
  runtime = acpx.createAcpRuntime({
    cwd,
    sessionStore,
    agentRegistry: registry,
    permissionMode: permissionTier === "full-development" ? "approve-all" : "approve-reads",
    nonInteractivePermissions: "fail",
    onPermissionRequest: async (request) => permissionDecision(
      permissionTier,
      approvalMode,
      request,
      approvalEndpoint,
      approvalToken,
    ),
    timeoutMs,
    probeAgent: agent,
    verbose: false,
  });
  const sessionOptions = typeof input.model === "string" && input.model.trim()
    ? { model: input.model.trim() }
    : undefined;
  handle = await runtime.ensureSession({
    sessionKey,
    agent,
    mode: "persistent",
    cwd,
    ...(sessionOptions ? { sessionOptions } : {}),
  });
  const capabilities = await runtime.getCapabilities({ handle });
  const sessionRecord = await sessionStore.load(handle.acpxRecordId ?? handle.sessionKey);
  const promptCapabilities = sanitizePromptCapabilities(sessionRecord?.agentCapabilities?.promptCapabilities);
  const initialStatus = await runtime.getStatus({ handle });
  emit({
    type: "session",
    agent,
    backend: handle.backend,
    runtimeSessionName: handle.runtimeSessionName,
    controls: Array.isArray(capabilities?.controls) ? capabilities.controls : [],
    promptCapabilities,
    permissionTier,
    permissionBoundary: permissionTier === "full-development"
      ? "session-trusted"
      : permissionTier === "edit"
        ? "scoped-edit"
        : "read-only",
    status: sanitizeStatus(initialStatus),
  });

  if (operation === "turn") {
    const text = requireString(input.text, "ACP turn text", 1_000_000);
    const parsedAttachments = parseAttachments(input.attachments);
    if (parsedAttachments.some((item) => item.kind === "image") && promptCapabilities.image !== true) {
      const error = new Error("official ACP Agent does not advertise native image input");
      error.code = "ACP_IMAGE_INPUT_UNSUPPORTED";
      throw error;
    }
    if (parsedAttachments.some((item) => item.kind === "audio") && promptCapabilities.audio !== true) {
      const error = new Error("official ACP Agent does not advertise native audio input");
      error.code = "ACP_AUDIO_INPUT_UNSUPPORTED";
      throw error;
    }
    const attachments = parsedAttachments.map(({ mediaType, data }) => ({ mediaType, data }));
    const requestId = requireString(input.requestId, "ACP request id", 200);
    const turn = runtime.startTurn({
      handle,
      text,
      attachments,
      mode: "prompt",
      requestId,
      timeoutMs,
    });
    emit({ type: "transport", status: "native_acp_content_submitted", attachmentCount: attachments.length });
    const resultPromise = turn.result;
    for await (const event of turn.events) {
      if (event?.type === "text_delta" && event.stream !== "thought" && event.tag !== "agent_thought_chunk") {
        if (typeof event.text === "string" && event.text) emit({ type: "text_delta", text: event.text });
      } else if (event?.type === "status") {
        const breakdown = sanitizeBreakdown(event.breakdown);
        const cost = sanitizeCost(event.cost);
        emit({
          type: "status",
          ...(typeof event.tag === "string" ? { tag: event.tag } : {}),
          ...(breakdown ? { usage: breakdown } : {}),
          ...(cost ? { cost } : {}),
        });
      } else if (event?.type === "tool_call") {
        emit({
          type: "tool_call",
          ...(typeof event.title === "string" ? { title: event.title.slice(0, 500) } : {}),
          ...(typeof event.status === "string" ? { status: event.status } : {}),
          ...(typeof event.kind === "string" ? { kind: event.kind } : {}),
        });
      }
    }
    const result = await resultPromise;
    if (result.status === "failed") {
      fail(result.error?.message, result.error?.code || "ACP_TURN_FAILED", result.error?.detailCode);
    } else {
      const finalStatus = await runtime.getStatus({ handle });
      emit({
        type: "done",
        status: result.status,
        ...(typeof result.stopReason === "string" ? { stopReason: result.stopReason } : {}),
        sessionStatus: sanitizeStatus(finalStatus),
      });
    }
  } else {
    emit({ type: "done", status: "ready" });
  }
} catch (error) {
  fail(error?.message || error, error?.code || "PEERBRIDGE_ACP_RUNTIME_FAILED", error?.detailCode);
} finally {
  if (runtime && handle) {
    try {
      await runtime.close({
        handle,
        reason: "PeerBridge bounded runtime bridge completed",
        discardPersistentState: false,
      });
    } catch {
      // A failed cleanup must not replace the turn result already emitted.
    }
  }
}
