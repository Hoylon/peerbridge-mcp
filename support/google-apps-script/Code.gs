const FEEDBACK_SCHEMA = "peerbridge.feedback-upload.v1";
const MAX_BUNDLE_BYTES = 8 * 1024 * 1024;
const MAX_ENVELOPE_CHARS = Math.ceil(MAX_BUNDLE_BYTES / 3) * 4 + 16 * 1024;
const MAX_NEW_CASES_PER_UTC_DAY = 20;
const PROTECTED_MAIL_QUOTA_RESERVE = 20;
const MAX_UNRESOLVED_DELIVERIES = 20;
const MAX_DELIVERY_ATTEMPTS = 3;
const MAX_AUTH_SKEW_MS = 10 * 60 * 1000;
const CASE_RETENTION_MS = 31 * 24 * 60 * 60 * 1000;
const INGRESS_SECRET_PROPERTY = "PEERBRIDGE_INGRESS_HMAC_SECRET";
const SUPPORT_EMAIL_PROPERTY = "PEERBRIDGE_SUPPORT_EMAIL";
const CASE_ID_PATTERN = /^[0-9a-f]{32}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const NONCE_PATTERN = /^[0-9a-f]{32}$/;
const TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const EMAIL_PATTERN = /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
const EXACT_ENVELOPE_FIELDS = [
  "schema",
  "case_id",
  "bundle_sha256",
  "bundle_base64",
  "reply_email",
  "received_utc",
  "ingress_timestamp",
  "ingress_nonce",
  "ingress_signature",
];

function jsonResponse_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function fail_(code) {
  return jsonResponse_({ ok: false, error: code });
}

function cleanLine_(value, limit) {
  return String(value || "")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, limit);
}

function hmacDigest_(text, secret) {
  return Utilities.computeHmacSha256Signature(text, secret)
    .map(function (value) {
      return ((value + 256) % 256).toString(16).padStart(2, "0");
    })
    .join("");
}

function constantTimeEqual_(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) {
    return false;
  }
  var difference = 0;
  for (var index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function bytesToHex_(bytes) {
  return bytes.map(function (value) {
    return ((value + 256) % 256).toString(16).padStart(2, "0");
  }).join("");
}

function decodeAndVerifyBundle_(encoded, expectedSha) {
  if (
    typeof encoded !== "string" || !encoded || encoded.length % 4 !== 0
    || encoded.length > Math.ceil(MAX_BUNDLE_BYTES / 3) * 4 + 4
    || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)
  ) {
    throw new Error("invalid_bundle_encoding");
  }
  var bytes;
  try {
    bytes = Utilities.base64Decode(encoded);
  } catch (_error) {
    throw new Error("invalid_bundle_encoding");
  }
  if (
    !bytes || bytes.length < 4 || bytes.length > MAX_BUNDLE_BYTES
    || Utilities.base64Encode(bytes) !== encoded
    || bytes[0] !== 0x50 || bytes[1] !== 0x4b || bytes[2] !== 0x03 || bytes[3] !== 0x04
  ) {
    throw new Error("invalid_bundle_encoding");
  }
  var observedSha = bytesToHex_(
    Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes)
  );
  if (!constantTimeEqual_(observedSha, expectedSha)) {
    throw new Error("bundle_sha256_mismatch");
  }
  return bytes;
}

function verifyIngress_(payload, properties, caseId, expectedSha) {
  var timestamp = String(payload.ingress_timestamp || "");
  var nonce = String(payload.ingress_nonce || "").toLowerCase();
  var signature = String(payload.ingress_signature || "").toLowerCase();
  var timestampMs = Date.parse(timestamp);
  if (
    !TIMESTAMP_PATTERN.test(timestamp) || !Number.isFinite(timestampMs)
    || Math.abs(Date.now() - timestampMs) > MAX_AUTH_SKEW_MS
    || !NONCE_PATTERN.test(nonce) || !SHA256_PATTERN.test(signature)
  ) {
    return false;
  }
  var secret = String(properties.getProperty(INGRESS_SECRET_PROPERTY) || "");
  if (secret.length < 43) return false;
  var canonical = [
    String(payload.schema || ""),
    caseId,
    expectedSha,
    timestamp,
    nonce,
    String(payload.reply_email || ""),
    String(payload.received_utc || ""),
  ].join("\n");
  return constantTimeEqual_(hmacDigest_(canonical, secret), signature);
}

function configureIngressSecret(secret) {
  var value = String(secret || "");
  if (value.length < 43 || value.length > 256 || /[\u0000-\u0020\u007f]/.test(value)) {
    throw new Error("Ingress secret must be a 43-256 character opaque value.");
  }
  PropertiesService.getScriptProperties().setProperty(INGRESS_SECRET_PROPERTY, value);
  return { configured: true };
}

function configureSupportEmail(email) {
  var value = String(email || "").trim();
  if (!EMAIL_PATTERN.test(value)) {
    throw new Error("Support email is invalid.");
  }
  PropertiesService.getScriptProperties().setProperty(SUPPORT_EMAIL_PROPERTY, value);
  return { configured: true };
}

function utcDay_() {
  return Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd");
}

function parseCaseState_(raw, nowMs) {
  if (!raw) return null;
  if (SHA256_PATTERN.test(raw)) {
    return {
      sha256: raw,
      state: "sent",
      created_ms: Number.isFinite(nowMs) && nowMs > 0 ? nowMs : Date.now(),
      migrated_legacy: true,
    };
  }
  try {
    var value = JSON.parse(raw);
    if (
      value && SHA256_PATTERN.test(String(value.sha256 || ""))
      && (
        value.state === "pending" || value.state === "uncertain"
        || value.state === "retry_ready" || value.state === "sent"
      )
    ) {
      if (!Number.isFinite(value.created_ms) || value.created_ms <= 0) {
        value.created_ms = Number.isFinite(nowMs) && nowMs > 0 ? nowMs : Date.now();
      }
      if (!Number.isFinite(value.updated_ms) || value.updated_ms <= 0) {
        value.updated_ms = value.created_ms;
      }
      if (!Number.isInteger(value.attempts) || value.attempts < 1) {
        value.attempts = 1;
      }
      return value;
    }
  } catch (_error) {
    return { invalid: true };
  }
  return { invalid: true };
}

function cleanupExpiredState_(properties, nowMs) {
  var cutoff = nowMs - CASE_RETENTION_MS;
  var values = properties.getProperties();
  Object.keys(values).forEach(function (key) {
    if (key.indexOf("case:") === 0) {
      var state = parseCaseState_(values[key], nowMs);
      if (state && state.migrated_legacy) {
        properties.setProperty(key, JSON.stringify({
          sha256: state.sha256,
          state: state.state,
          created_ms: state.created_ms,
        }));
      }
      if (
        state && !state.invalid && state.state === "sent"
        && state.updated_ms > 0 && state.updated_ms < cutoff
      ) {
        properties.deleteProperty(key);
      }
    } else if (key.indexOf("daily:") === 0) {
      var dayMs = Date.parse(key.slice(6) + "T00:00:00.000Z");
      if (Number.isFinite(dayMs) && dayMs < cutoff) properties.deleteProperty(key);
    }
  });
}

function unresolvedDeliveryCount_(properties, nowMs) {
  var values = properties.getProperties();
  return Object.keys(values).filter(function (key) {
    if (key.indexOf("case:") !== 0) return false;
    var state = parseCaseState_(values[key], nowMs);
    return !state || state.invalid || state.state !== "sent";
  }).length;
}

function deliveryState_(sha256, state, createdMs, updatedMs, attempts, lastError) {
  var value = {
    sha256: sha256,
    state: state,
    created_ms: createdMs,
    updated_ms: updatedMs,
    attempts: attempts,
  };
  if (lastError) value.last_error = lastError;
  return JSON.stringify(value);
}

function recordUncertainDelivery_(properties, caseKey, state, nowMs, errorCode) {
  try {
    properties.setProperty(caseKey, deliveryState_(
      state.sha256,
      "uncertain",
      state.created_ms,
      nowMs,
      state.attempts,
      errorCode
    ));
  } catch (_error) {
    // The pre-send pending marker remains the fail-closed recovery record.
  }
}

function operatorResult_(value) {
  console.log(JSON.stringify(value));
  return value;
}

function listDeliveryReconciliation() {
  var properties = PropertiesService.getScriptProperties();
  var nowMs = Date.now();
  var items = [];
  var values = properties.getProperties();
  Object.keys(values).forEach(function (key) {
    if (key.indexOf("case:") !== 0) return;
    var state = parseCaseState_(values[key], nowMs);
    if (!state || state.invalid || state.state === "sent") return;
    items.push({
      case_id: key.slice(5),
      bundle_sha256: state.sha256,
      state: state.state,
      attempts: state.attempts,
      attempts_remaining: Math.max(0, MAX_DELIVERY_ATTEMPTS - state.attempts),
      created_utc: new Date(state.created_ms).toISOString(),
      updated_utc: new Date(state.updated_ms).toISOString(),
      last_error: String(state.last_error || "delivery_interrupted"),
    });
  });
  items.sort(function (left, right) {
    return left.created_utc.localeCompare(right.created_utc)
      || left.case_id.localeCompare(right.case_id);
  });
  return operatorResult_({
    generated_utc: new Date(nowMs).toISOString(),
    unresolved_count: items.length,
    unresolved_limit: MAX_UNRESOLVED_DELIVERIES,
    max_delivery_attempts: MAX_DELIVERY_ATTEMPTS,
    deliveries: items,
  });
}

function reconcileDelivery(caseId, expectedSha, action) {
  var normalizedCaseId = String(caseId || "").toLowerCase();
  var normalizedSha = String(expectedSha || "").toLowerCase();
  var normalizedAction = String(action || "");
  if (!CASE_ID_PATTERN.test(normalizedCaseId) || !SHA256_PATTERN.test(normalizedSha)) {
    throw new Error("Exact case ID and bundle SHA-256 are required.");
  }
  if (normalizedAction !== "mark_delivered" && normalizedAction !== "allow_retry") {
    throw new Error("Action must be mark_delivered or allow_retry.");
  }

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) throw new Error("Receiver is busy; retry reconciliation.");
  try {
    var properties = PropertiesService.getScriptProperties();
    var caseKey = "case:" + normalizedCaseId;
    var nowMs = Date.now();
    var state = parseCaseState_(properties.getProperty(caseKey), nowMs);
    if (!state || state.invalid || state.sha256 !== normalizedSha) {
      throw new Error("Case state does not match the supplied identity.");
    }
    if (state.state === "sent") {
      if (normalizedAction !== "mark_delivered") {
        throw new Error("A delivered case cannot be retried.");
      }
      return operatorResult_({
        case_id: normalizedCaseId,
        bundle_sha256: normalizedSha,
        state: "sent",
      });
    }
    if (normalizedAction === "mark_delivered") {
      properties.setProperty(caseKey, deliveryState_(
        normalizedSha, "sent", state.created_ms, nowMs, state.attempts, ""
      ));
      return operatorResult_({
        case_id: normalizedCaseId,
        bundle_sha256: normalizedSha,
        state: "sent",
      });
    }
    if (state.state === "retry_ready") {
      return operatorResult_({
        case_id: normalizedCaseId,
        bundle_sha256: normalizedSha,
        state: "retry_ready",
        attempts_remaining: MAX_DELIVERY_ATTEMPTS - state.attempts,
      });
    }
    if (state.attempts >= MAX_DELIVERY_ATTEMPTS) {
      throw new Error("Delivery attempt limit reached; submit a new case if mailbox inspection confirms no delivery.");
    }
    properties.setProperty(caseKey, deliveryState_(
      normalizedSha,
      "retry_ready",
      state.created_ms,
      nowMs,
      state.attempts,
      state.last_error || "delivery_interrupted"
    ));
    return operatorResult_({
      case_id: normalizedCaseId,
      bundle_sha256: normalizedSha,
      state: "retry_ready",
      attempts_remaining: MAX_DELIVERY_ATTEMPTS - state.attempts,
    });
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return jsonResponse_({
    ok: true,
    schema: FEEDBACK_SCHEMA,
    service: "PeerBridge private feedback intake",
  });
}

function doPost(event) {
  if (!event || !event.postData || typeof event.postData.contents !== "string") {
    return fail_("missing_body");
  }
  if (event.postData.contents.length > MAX_ENVELOPE_CHARS) {
    return fail_("request_too_large");
  }

  var payload;
  try {
    payload = JSON.parse(event.postData.contents);
  } catch (_error) {
    return fail_("invalid_json");
  }
  if (!payload || payload.schema !== FEEDBACK_SCHEMA) {
    return fail_("invalid_schema");
  }
  var fields = Object.keys(payload).sort();
  var expectedFields = EXACT_ENVELOPE_FIELDS.slice().sort();
  if (
    fields.length !== expectedFields.length
    || fields.some(function (field, index) { return field !== expectedFields[index]; })
  ) {
    return fail_("invalid_envelope_fields");
  }

  var caseId = String(payload.case_id || "").toLowerCase();
  var expectedSha = String(payload.bundle_sha256 || "").toLowerCase();
  if (!CASE_ID_PATTERN.test(caseId) || !SHA256_PATTERN.test(expectedSha)) {
    return fail_("invalid_identity");
  }
  var properties = PropertiesService.getScriptProperties();
  if (!verifyIngress_(payload, properties, caseId, expectedSha)) {
    return fail_("unauthorized_ingress");
  }
  var bundleBytes;
  try {
    bundleBytes = decodeAndVerifyBundle_(payload.bundle_base64, expectedSha);
  } catch (error) {
    return fail_(String(error.message || "invalid_bundle"));
  }
  var receivedUtc = String(payload.received_utc || "");
  if (!TIMESTAMP_PATTERN.test(receivedUtc) || !Number.isFinite(Date.parse(receivedUtc))) {
    return fail_("invalid_received_timestamp");
  }

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    return fail_("receiver_busy");
  }
  try {
    var nowMs = Date.now();
    cleanupExpiredState_(properties, nowMs);
    var caseKey = "case:" + caseId;
    var priorState = parseCaseState_(properties.getProperty(caseKey), nowMs);
    var isOperatorApprovedRetry = false;
    if (priorState) {
      if (priorState.invalid || priorState.sha256 !== expectedSha) {
        return fail_("case_conflict");
      }
      if (priorState.state === "pending" || priorState.state === "uncertain") {
        return fail_("delivery_state_uncertain");
      }
      if (priorState.state === "sent") {
        return jsonResponse_({
          ok: true,
          case_id: caseId,
          bundle_sha256: expectedSha,
          receipt: "duplicate_already_received",
        });
      }
      if (priorState.attempts >= MAX_DELIVERY_ATTEMPTS) {
        return fail_("delivery_attempt_limit_reached");
      }
      isOperatorApprovedRetry = true;
    }

    var dayKey = "daily:" + utcDay_();
    var dayCount = Number(properties.getProperty(dayKey) || "0");
    if (!Number.isFinite(dayCount) || dayCount < 0) {
      dayCount = 0;
    }
    if (!isOperatorApprovedRetry && dayCount >= MAX_NEW_CASES_PER_UTC_DAY) {
      return fail_("daily_quota_reached");
    }
    var remainingMailQuota = Number(MailApp.getRemainingDailyQuota());
    if (
      !Number.isFinite(remainingMailQuota)
      || remainingMailQuota <= PROTECTED_MAIL_QUOTA_RESERVE
    ) {
      return fail_("protected_notification_capacity_reserved");
    }
    if (!isOperatorApprovedRetry && unresolvedDeliveryCount_(properties, nowMs) >= MAX_UNRESOLVED_DELIVERIES) {
      return fail_("delivery_reconciliation_required");
    }

    var replyEmail = cleanLine_(payload.reply_email, 320);
    if (!EMAIL_PATTERN.test(replyEmail)) {
      replyEmail = "";
    }
    var createdMs = isOperatorApprovedRetry ? priorState.created_ms : nowMs;
    var attemptCount = isOperatorApprovedRetry ? priorState.attempts + 1 : 1;
    var pendingState = deliveryState_(
      expectedSha, "pending", createdMs, nowMs, attemptCount, ""
    );
    var pendingProperties = {};
    pendingProperties[caseKey] = pendingState;
    if (!isOperatorApprovedRetry) pendingProperties[dayKey] = String(dayCount + 1);
    properties.setProperties(pendingProperties, false);
    var activeState = parseCaseState_(pendingState, nowMs);
    var body = [
      "PeerBridge private feedback received.",
      "",
      "Reply address: " + (replyEmail || "not provided"),
      "",
      "Case: " + caseId,
      "Bundle SHA-256: " + expectedSha,
      "Received (UTC): " + receivedUtc,
      "",
      "The validated private feedback bundle is attached.",
      "Any full API key/token inside the bundle was encrypted locally to the configured maintainer support public key before upload.",
    ].join("\n");
    var supportEmail = String(properties.getProperty(SUPPORT_EMAIL_PROPERTY) || "").trim();
    if (!EMAIL_PATTERN.test(supportEmail)) {
      return fail_("support_destination_unavailable");
    }
    var message = {
      to: supportEmail,
      subject: "PeerBridge feedback " + caseId,
      body: body,
      name: "PeerBridge Feedback",
      attachments: [Utilities.newBlob(
        bundleBytes,
        "application/zip",
        "peerbridge-feedback-" + caseId + ".zip"
      )],
    };
    if (replyEmail) {
      message.replyTo = replyEmail;
    }

    try {
      MailApp.sendEmail(message);
    } catch (_error) {
      recordUncertainDelivery_(properties, caseKey, activeState, Date.now(), "send_email_exception");
      return fail_("delivery_failed");
    }
    try {
      properties.setProperty(caseKey, deliveryState_(
        expectedSha, "sent", createdMs, Date.now(), attemptCount, ""
      ));
    } catch (_error) {
      recordUncertainDelivery_(properties, caseKey, activeState, Date.now(), "sent_state_write_failed");
      return fail_("delivery_failed");
    }
    return jsonResponse_({
      ok: true,
      case_id: caseId,
      bundle_sha256: expectedSha,
      receipt: "emailed_to_private_support",
    });
  } catch (_error) {
    return fail_("delivery_failed");
  } finally {
    lock.releaseLock();
  }
}
