import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../Code.gs", import.meta.url), "utf8");
const manifest = JSON.parse(
  fs.readFileSync(new URL("../appsscript.json", import.meta.url), "utf8"),
);
const TEST_INGRESS_SECRET = "test-ingress-secret-0123456789-ABCDEFGHIJKLMNO";
const TEST_BUNDLE = Buffer.concat([
  Buffer.from([0x50, 0x4b, 0x03, 0x04]),
  Buffer.from("peerbridge-private-feedback", "utf8"),
]);
const TEST_BUNDLE_BASE64 = TEST_BUNDLE.toString("base64");
const TEST_BUNDLE_SHA256 = crypto.createHash("sha256").update(TEST_BUNDLE).digest("hex");

function signedBytes(value) {
  return [...Buffer.from(value)].map((item) => (item > 127 ? item - 256 : item));
}

function unsignedBuffer(value) {
  return Buffer.from([...value].map((item) => (item + 256) % 256));
}

function receiver({ failSentStateWrites = 0, sendEmailFailures = 0 } = {}) {
  const properties = new Map([
    ["PEERBRIDGE_INGRESS_HMAC_SECRET", TEST_INGRESS_SECRET],
    ["PEERBRIDGE_SUPPORT_EMAIL", "maintainer@example.com"],
  ]);
  const sent = [];
  const mailAttempts = [];
  const operatorLogs = [];
  let remainingSendFailures = sendEmailFailures;
  let remainingSentStateWriteFailures = failSentStateWrites;
  const context = {
    JSON,
    Date,
    Number,
    String,
    console: {
      log: (value) => operatorLogs.push(String(value)),
    },
    ContentService: {
      MimeType: { JSON: "application/json" },
      createTextOutput(text) {
        return {
          text,
          mimeType: null,
          setMimeType(value) {
            this.mimeType = value;
            return this;
          },
        };
      },
    },
    LockService: {
      getScriptLock() {
        return { tryLock: () => true, releaseLock: () => undefined };
      },
    },
    MailApp: {
      getRemainingDailyQuota: () => 100,
      sendEmail(message) {
        mailAttempts.push(message);
        if (remainingSendFailures > 0) {
          remainingSendFailures -= 1;
          throw new Error("synthetic sendEmail failure");
        }
        sent.push(message);
      },
    },
    PropertiesService: {
      getScriptProperties() {
        return {
          getProperty: (key) => properties.get(key) ?? null,
          getProperties: () => Object.fromEntries(properties),
          deleteProperty: (key) => properties.delete(key),
          setProperties: (values) => {
            for (const [key, value] of Object.entries(values)) properties.set(key, value);
          },
          setProperty: (key, value) => {
            if (
              remainingSentStateWriteFailures > 0
              && key.startsWith("case:")
              && String(value).includes('"state":"sent"')
            ) {
              remainingSentStateWriteFailures -= 1;
              throw new Error("synthetic property write failure");
            }
            properties.set(key, value);
          },
        };
      },
    },
    Utilities: {
      DigestAlgorithm: { SHA_256: "SHA_256" },
      base64Decode: (value) => signedBytes(Buffer.from(value, "base64")),
      base64Encode: (value) => unsignedBuffer(value).toString("base64"),
      computeDigest: (_algorithm, value) => signedBytes(
        crypto.createHash("sha256").update(unsignedBuffer(value)).digest(),
      ),
      computeHmacSha256Signature: (value, key) => [
        ...crypto.createHmac("sha256", key).update(value).digest(),
      ].map((item) => (item > 127 ? item - 256 : item)),
      formatDate: () => "2026-08-17",
      newBlob: (value, contentType, name) => ({
        bytes: unsignedBuffer(value),
        contentType,
        name,
      }),
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, mailAttempts, operatorLogs, properties, sent };
}

function payload(overrides = {}) {
  const caseId = String(overrides.case_id || "a".repeat(32));
  const result = {
    schema: "peerbridge.feedback-upload.v1",
    case_id: caseId,
    bundle_sha256: TEST_BUNDLE_SHA256,
    bundle_base64: TEST_BUNDLE_BASE64,
    reply_email: "reporter@example.com",
    received_utc: "2026-08-17T00:00:00.000Z",
    ...overrides,
  };
  result.ingress_timestamp ??= new Date().toISOString();
  result.ingress_nonce ??= "b".repeat(32);
  result.ingress_signature ??= crypto
    .createHmac("sha256", TEST_INGRESS_SECRET)
    .update([
      result.schema,
      result.case_id,
      result.bundle_sha256,
      result.ingress_timestamp,
      result.ingress_nonce,
      result.reply_email,
      result.received_utc,
    ].join("\n"))
    .digest("hex");
  return result;
}

function post(context, body) {
  const response = context.doPost({ postData: { contents: JSON.stringify(body) } });
  return JSON.parse(response.text);
}

function rawPost(context, contents) {
  const response = context.doPost({ postData: { contents } });
  return JSON.parse(response.text);
}

test("web app accepts public submissions but always executes as the maintainer", () => {
  assert.deepEqual(manifest.webapp, {
    access: "ANYONE_ANONYMOUS",
    executeAs: "USER_DEPLOYING",
  });
});

test("sends the verified private ZIP to the configured maintainer mailbox", () => {
  const { context, sent } = receiver();
  const body = payload();
  const result = post(context, body);

  assert.equal(result.ok, true);
  assert.equal(result.case_id, body.case_id);
  assert.equal(result.bundle_sha256, body.bundle_sha256);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, "maintainer@example.com");
  assert.equal(sent[0].replyTo, "reporter@example.com");
  assert.equal(sent[0].attachments.length, 1);
  assert.equal(sent[0].attachments[0].contentType, "application/zip");
  assert.equal(sent[0].attachments[0].name, `peerbridge-feedback-${body.case_id}.zip`);
  assert.deepEqual(sent[0].attachments[0].bytes, TEST_BUNDLE);
  assert.match(sent[0].body, /Reply address: reporter@example\.com/u);
  assert.match(sent[0].body, /Received \(UTC\): 2026-08-17T00:00:00\.000Z/u);
  assert.match(sent[0].body, /validated private feedback bundle is attached/u);
  assert.match(sent[0].body, /encrypted locally/u);
  assert.doesNotMatch(sent[0].body, /Synthetic parser failure/u);
});

test("rejects every unrecognized envelope field even when the known fields are signed", () => {
  const { context, sent } = receiver();
  const result = post(context, payload({ arbitrary_nested: { secret: "must-not-pass" } }));
  assert.deepEqual(result, { ok: false, error: "invalid_envelope_fields" });
  assert.equal(sent.length, 0);
});

test("rejects direct unsigned traffic before mailing", () => {
  const { context, sent } = receiver();
  const body = payload();
  delete body.ingress_signature;
  const result = post(context, body);
  assert.deepEqual(result, { ok: false, error: "invalid_envelope_fields" });
  assert.equal(sent.length, 0);
});

test("rejects signed-envelope metadata changed after signing", () => {
  for (const [field, value] of [
    ["reply_email", "tampered@example.com"],
    ["bundle_sha256", "d".repeat(64)],
    ["received_utc", "2026-08-17T00:00:01.000Z"],
  ]) {
    const { context, sent } = receiver();
    const body = payload();
    body[field] = value;
    const result = post(context, body);
    assert.deepEqual(result, { ok: false, error: "unauthorized_ingress" });
    assert.equal(sent.length, 0);
  }
});

test("same case and SHA is idempotent while a conflicting case is rejected", () => {
  const { context, sent } = receiver();
  const body = payload();
  assert.equal(post(context, body).receipt, "emailed_to_private_support");
  assert.equal(post(context, body).receipt, "duplicate_already_received");
  assert.equal(sent.length, 1);

  const conflict = payload({
    bundle_base64: Buffer.concat([TEST_BUNDLE, Buffer.from("different")]).toString("base64"),
    bundle_sha256: crypto.createHash("sha256")
      .update(Buffer.concat([TEST_BUNDLE, Buffer.from("different")]))
      .digest("hex"),
  });
  assert.deepEqual(post(context, conflict), { ok: false, error: "case_conflict" });
  assert.equal(sent.length, 1);
});

test("invalid reply address is omitted instead of becoming a mail header", () => {
  const { context, sent } = receiver();
  const result = post(context, payload({ reply_email: "victim@example.com\nBcc: attacker@example.com" }));
  assert.equal(result.ok, true);
  assert.equal(Object.hasOwn(sent[0], "replyTo"), false);
});

test("rejects every non-contract field instead of forwarding it through Google", () => {
  for (const [field, value] of [
    ["bundle", "private bytes"],
    ["attachments", ["private.png"]],
    ["diagnostics", "private diagnostics"],
    ["encrypted_credential", "ciphertext"],
    ["summary", "caller-controlled secret"],
    ["app_version", "caller-controlled version"],
  ]) {
    const { context, sent } = receiver();
    const result = post(context, payload({ [field]: value }));
    assert.deepEqual(result, { ok: false, error: "invalid_envelope_fields" });
    assert.equal(sent.length, 0);
  }
});

test("rejects malformed bundle encoding and a bundle SHA mismatch", () => {
  {
    const { context, sent } = receiver();
    const result = post(context, payload({ bundle_base64: "not-base64" }));
    assert.deepEqual(result, { ok: false, error: "invalid_bundle_encoding" });
    assert.equal(sent.length, 0);
  }
  {
    const { context, sent } = receiver();
    const result = post(context, payload({ bundle_sha256: "d".repeat(64) }));
    assert.deepEqual(result, { ok: false, error: "bundle_sha256_mismatch" });
    assert.equal(sent.length, 0);
  }
});

test("a sendEmail exception is operator-visible and never retries automatically", () => {
  const {
    context, mailAttempts, operatorLogs, properties, sent,
  } = receiver({ sendEmailFailures: 1 });
  const body = payload();

  assert.deepEqual(post(context, body), { ok: false, error: "delivery_failed" });
  assert.equal(mailAttempts.length, 1);
  assert.equal(sent.length, 0);
  assert.deepEqual(post(context, body), {
    ok: false,
    error: "delivery_state_uncertain",
  });
  assert.equal(mailAttempts.length, 1);

  const pending = context.listDeliveryReconciliation();
  assert.equal(pending.unresolved_count, 1);
  assert.equal(pending.deliveries[0].case_id, body.case_id);
  assert.equal(pending.deliveries[0].bundle_sha256, body.bundle_sha256);
  assert.equal(pending.deliveries[0].state, "uncertain");
  assert.equal(pending.deliveries[0].attempts, 1);
  assert.equal(pending.deliveries[0].attempts_remaining, 2);
  assert.equal(pending.deliveries[0].last_error, "send_email_exception");
  assert.doesNotMatch(JSON.stringify(pending), /reporter@example\.com|bundle_base64/u);
  assert.deepEqual(JSON.parse(operatorLogs.at(-1)), JSON.parse(JSON.stringify(pending)));

  const armed = context.reconcileDelivery(body.case_id, body.bundle_sha256, "allow_retry");
  assert.equal(armed.state, "retry_ready");
  assert.equal(post(context, body).receipt, "emailed_to_private_support");
  assert.equal(mailAttempts.length, 2);
  assert.equal(sent.length, 1);
  assert.equal(properties.get("daily:2026-08-17"), "1");
  assert.equal(JSON.parse(properties.get(`case:${body.case_id}`)).attempts, 2);
  assert.equal(post(context, body).receipt, "duplicate_already_received");
  assert.equal(mailAttempts.length, 2);
});

test("a post-send state failure can be marked delivered without another email", () => {
  const { context, mailAttempts, sent } = receiver({ failSentStateWrites: 1 });
  const body = payload();
  assert.deepEqual(post(context, body), { ok: false, error: "delivery_failed" });
  assert.equal(sent.length, 1);
  assert.equal(context.listDeliveryReconciliation().deliveries[0].last_error, "sent_state_write_failed");
  assert.deepEqual(post(context, body), {
    ok: false,
    error: "delivery_state_uncertain",
  });
  assert.equal(sent.length, 1);
  assert.throws(
    () => context.reconcileDelivery(body.case_id, "d".repeat(64), "mark_delivered"),
    /does not match/u,
  );
  const reconciled = context.reconcileDelivery(
    body.case_id,
    body.bundle_sha256,
    "mark_delivered",
  );
  assert.equal(reconciled.state, "sent");
  assert.equal(post(context, body).receipt, "duplicate_already_received");
  assert.equal(mailAttempts.length, 1);
  assert.equal(sent.length, 1);
});

test("manual retry attempts are capped", () => {
  const { context, mailAttempts, properties, sent } = receiver({ sendEmailFailures: 3 });
  const body = payload();

  assert.equal(post(context, body).error, "delivery_failed");
  for (let attempt = 2; attempt <= 3; attempt += 1) {
    assert.equal(
      context.reconcileDelivery(body.case_id, body.bundle_sha256, "allow_retry").state,
      "retry_ready",
    );
    assert.equal(post(context, body).error, "delivery_failed");
  }
  assert.throws(
    () => context.reconcileDelivery(body.case_id, body.bundle_sha256, "allow_retry"),
    /attempt limit reached/u,
  );
  assert.equal(mailAttempts.length, 3);
  assert.equal(sent.length, 0);
  assert.equal(JSON.parse(properties.get(`case:${body.case_id}`)).attempts, 3);
  assert.equal(context.listDeliveryReconciliation().deliveries[0].attempts_remaining, 0);
});

test("unresolved delivery records are bounded and never silently expired", () => {
  const { context, mailAttempts, properties } = receiver();
  const now = Date.now();
  const limit = vm.runInContext("MAX_UNRESOLVED_DELIVERIES", context);
  for (let index = 0; index < limit; index += 1) {
    const caseId = index.toString(16).padStart(32, "0");
    properties.set(`case:${caseId}`, JSON.stringify({
      sha256: TEST_BUNDLE_SHA256,
      state: "uncertain",
      created_ms: now,
      updated_ms: now,
      attempts: 1,
      last_error: "send_email_exception",
    }));
  }

  const retention = vm.runInContext("CASE_RETENTION_MS", context);
  context.cleanupExpiredState_(
    context.PropertiesService.getScriptProperties(),
    now + retention + 1,
  );
  assert.equal(context.listDeliveryReconciliation().unresolved_count, limit);
  assert.deepEqual(post(context, payload({ case_id: "f".repeat(32) })), {
    ok: false,
    error: "delivery_reconciliation_required",
  });
  assert.equal(mailAttempts.length, 0);
});

test("rejects an oversized request envelope before parsing", () => {
  const { context, sent } = receiver();
  const maximum = vm.runInContext("MAX_ENVELOPE_CHARS", context);
  const result = rawPost(context, "x".repeat(maximum + 1));
  assert.deepEqual(result, { ok: false, error: "request_too_large" });
  assert.equal(sent.length, 0);
});

test("legacy idempotency state is timestamped once and then expires", () => {
  const { context, properties } = receiver();
  const caseId = "a".repeat(32);
  const caseKey = `case:${caseId}`;
  const sha256 = "c".repeat(64);
  properties.set(caseKey, sha256);
  const scriptProperties = context.PropertiesService.getScriptProperties();
  const firstSeen = Date.parse("2026-08-17T00:00:00.000Z");

  context.cleanupExpiredState_(scriptProperties, firstSeen);
  const migrated = JSON.parse(properties.get(caseKey));
  assert.deepEqual(migrated, {
    sha256,
    state: "sent",
    created_ms: firstSeen,
  });

  const retention = vm.runInContext("CASE_RETENTION_MS", context);
  context.cleanupExpiredState_(scriptProperties, firstSeen + retention + 1);
  assert.equal(properties.has(caseKey), false);
});
