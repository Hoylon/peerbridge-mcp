import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { deflateRawSync } from "node:zlib";

import { handleRequest, testing } from "../src/index.js";


const VALID_IMAGES = new Map([
  ["attachments/01.png", Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64")],
  ["attachments/01.jpg", Buffer.from("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EH//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EH//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EH//2Q==", "base64")],
  ["attachments/01.gif", Buffer.from("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==", "base64")],
  ["attachments/01.webp", Buffer.from("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEAAUAmJaQAA3AA/v89", "base64")],
]);


function crc32(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
    }
  }
  return (value ^ 0xffffffff) >>> 0;
}

function zipEntries(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name, "ascii");
    const payload = Buffer.from(entry.payload);
    const compressed = deflateRawSync(payload);
    const checksum = crc32(payload);
    const local = Buffer.alloc(30 + name.length);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(8, 8);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(payload.length, 22);
    local.writeUInt16LE(name.length, 26);
    name.copy(local, 30);
    locals.push(local, compressed);

    const central = Buffer.alloc(46 + name.length);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(0x0314, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(8, 10);
    central.writeUInt32LE(checksum, 16);
    central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(payload.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt32LE((0o100600 << 16) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    name.copy(central, 46);
    centrals.push(central);
    offset += local.length + compressed.length;
  }
  const centralBytes = Buffer.concat(centrals);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(entries.length, 8);
  eocd.writeUInt16LE(entries.length, 10);
  eocd.writeUInt32LE(centralBytes.length, 12);
  eocd.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, centralBytes, eocd]);
}

function validEncryptedEnvelope(caseId, overrides = {}) {
  return {
    schema: "peerbridge.feedback-secret-envelope.v1",
    case_id: caseId,
    algorithm: "RSA-OAEP-SHA256+A256GCM",
    public_key_sha256: "d".repeat(64),
    associated_data_b64: Buffer.from(
      `peerbridge.feedback-secret-envelope.v1:${caseId}`,
      "ascii",
    ).toString("base64"),
    wrapped_key_b64: Buffer.alloc(384, 0x31).toString("base64"),
    nonce_b64: Buffer.alloc(12, 0x32).toString("base64"),
    ciphertext_b64: Buffer.alloc(32, 0x33).toString("base64"),
    ...overrides,
  };
}

function feedbackZip(caseId, {
  encrypted = false,
  encryptedEnvelope = null,
  attachments = [],
  extras = [],
  summary = "Synthetic parser failure",
  contact = "reporter@example.com",
  appVersion = "0.1.0",
} = {}) {
  const attachmentManifest = attachments.map((entry, index) => ({
    archive_name: entry.name,
    original_name: `attachment-${index + 1}`,
    bytes: Buffer.byteLength(entry.payload),
    sha256: createHash("sha256").update(entry.payload).digest("hex"),
  }));
  const report = {
    schema: "peerbridge.feedback-report.v1",
    case_id: caseId,
    created_utc: "2026-08-17T00:00:00Z",
    summary,
    message: "Synthetic bounded report",
    contact,
    runtime: { app_version: appVersion },
    encrypted_credential_included: encrypted,
    attachments: attachmentManifest,
  };
  return zipEntries([
    { name: "report.json", payload: JSON.stringify(report) },
    ...(encrypted ? [{
      name: "encrypted-credential.json",
      payload: JSON.stringify(encryptedEnvelope || validEncryptedEnvelope(caseId)),
    }] : []),
    ...attachments,
    ...extras,
  ]);
}

function baseEnv(overrides = {}) {
  return {
    DB: {},
    BUNDLES: {},
    RATE_SALT: "test-rate-salt-not-for-production",
    ADMIN_TOKEN: "test-admin-token-not-for-production",
    ...overrides,
  };
}

test("Worker fails closed when an administrative secret is weak", async () => {
  for (const overrides of [
    { ADMIN_TOKEN: "short" },
    { RATE_SALT: "short" },
    { ADMIN_TOKEN: "x".repeat(31) },
    { RATE_SALT: `valid-length-but-has whitespace ${"x".repeat(8)}` },
  ]) {
    const response = await handleRequest(
      new Request("https://edge.example/health"),
      baseEnv(overrides),
    );
    assert.equal(response.status, 503);
    const payload = await response.json();
    assert.equal(payload.ok, false);
    assert.equal(payload.error, "service_unavailable");
    assert.equal(payload.message, "Service temporarily unavailable.");
    assert.match(payload.incident_id, /^[0-9a-f-]{36}$/u);
  }
});


test("health exposes the non-secret independent R2 lifecycle requirement", async () => {
  const response = await handleRequest(
    new Request("https://edge.example/health"),
    baseEnv({
      BUNDLES: {
        put: async () => {},
        get: async () => null,
        delete: async () => {},
      },
    }),
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.bindings.bundles, true);
  assert.deepEqual(payload.retention, {
    d1_days: 30,
    r2_lifecycle_rule: {
      required: true,
      prefix: "feedback/",
      expiration_days: 30,
    },
  });

  const missing = await handleRequest(
    new Request("https://edge.example/health"),
    baseEnv({ BUNDLES: undefined }),
  );
  assert.equal((await missing.json()).bindings.bundles, false);
});


function memoryIntakeEnv({
  putError = null,
  deleteError = null,
  insertError = null,
  commitThenThrow = false,
  beforePut = null,
} = {}) {
  const rates = new Map();
  const cases = new Map();
  const objects = new Map();
  const cleanups = new Map();
  const deleted = [];
  const DB = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            sql,
            args,
            async all() {
              if (sql.includes("FROM feedback_cases WHERE notification_status")) {
                const [maximumAttempts, now, limit] = args;
                return {
                  results: [...cases.values()]
                    .filter((row) => ["pending", "failed"].includes(row.notification_status))
                    .filter((row) => row.notification_attempt_count < maximumAttempts)
                    .filter((row) => (
                      !row.notification_claim_token_sha256
                      || row.notification_claim_expires_utc <= now
                    ))
                    .sort((left, right) => String(
                      left.notification_last_attempt_utc || left.received_utc,
                    ).localeCompare(String(
                      right.notification_last_attempt_utc || right.received_utc,
                    )))
                    .slice(0, limit),
                };
              }
              if (!sql.includes("FROM rate_limits")) throw new Error("unexpected all query");
              const [day, ...rateKeys] = args;
              return {
                results: rateKeys.map((rateKey) => ({
                  rate_key: rateKey,
                  request_count: rates.get(`${day}:${rateKey}`) || 0,
                })),
              };
            },
            async first() {
              if (!sql.includes("FROM feedback_cases")) throw new Error("unexpected first query");
              if (sql.includes("WHERE object_key = ?")) {
                return [...cases.values()].find((row) => row.object_key === args[0]) || null;
              }
              return cases.get(args[0]) || null;
            },
            async run() {
              if (sql.includes("INSERT INTO feedback_object_cleanup")) {
                cleanups.set(args[0], {
                  object_key: args[0],
                  case_id: args[1],
                  bundle_sha256: args[2],
                  reason: args[3],
                  created_utc: args[4],
                  updated_utc: args[5],
                  attempt_count: 0,
                });
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE feedback_cases SET notification_status")) {
                const row = cases.get(args[4]);
                if (
                  !row || !["pending", "failed"].includes(row.notification_status)
                  || row.notification_claim_token_sha256 !== args[5]
                ) {
                  return { meta: { changes: 0 } };
                }
                row.notification_status = args[0];
                row.notification_attempt_count += 1;
                row.notification_last_attempt_utc = args[1];
                if (args[2] === 1 && !row.notification_sent_utc) {
                  row.notification_sent_utc = args[3];
                }
                row.notification_claim_token_sha256 = null;
                row.notification_claim_expires_utc = null;
                return { meta: { changes: 1 } };
              }
              if (sql.startsWith("UPDATE feedback_cases SET notification_claim_token_sha256")) {
                const [claimTokenSha256, claimExpiresUtc, caseId, maximumAttempts, now] = args;
                const row = cases.get(caseId);
                if (
                  !row || !["pending", "failed"].includes(row.notification_status)
                  || row.notification_attempt_count >= maximumAttempts
                  || (
                    row.notification_claim_token_sha256
                    && row.notification_claim_expires_utc > now
                  )
                ) {
                  return { meta: { changes: 0 } };
                }
                row.notification_claim_token_sha256 = claimTokenSha256;
                row.notification_claim_expires_utc = claimExpiresUtc;
                return { meta: { changes: 1 } };
              }
              if (!sql.includes("INSERT INTO feedback_cases")) {
                throw new Error("unexpected run query");
              }
              const row = {
                case_id: args[0],
                bundle_sha256: args[1],
                submission_id: args[2],
                object_key: args[3],
                summary: args[4],
                reply_email: args[5],
                app_version: args[6],
                received_utc: args[8],
                notification_status: args[10],
                notification_attempt_count: args[11],
                notification_last_attempt_utc: args[12],
                notification_sent_utc: args[13],
                notification_claim_token_sha256: null,
                notification_claim_expires_utc: null,
              };
              if (commitThenThrow) {
                cases.set(args[0], row);
                throw new Error("ambiguous D1 acknowledgement");
              }
              if (insertError) throw insertError;
              if (cases.has(args[0])) throw new Error("feedback case unique constraint failed");
              cases.set(args[0], row);
              return { success: true };
            },
          };
        },
      };
    },
    async batch(statements) {
      const next = new Map(rates);
      for (const statement of statements) {
        if (statement.sql.startsWith("INSERT INTO rate_limits")) {
          const [rateKey, day] = statement.args;
          const mapKey = `${day}:${rateKey}`;
          const count = (next.get(mapKey) || 0) + 1;
          if (rateKey.startsWith("attempt-source:") && count > 20) {
            throw new Error("source attempt rate limit exceeded");
          }
          if (rateKey === "attempt-global" && count > 500) {
            throw new Error("global attempt rate limit exceeded");
          }
          if (rateKey.startsWith("accepted-source:") && count > 5) {
            throw new Error("accepted source rate limit exceeded");
          }
          if (rateKey === "accepted-global" && count > 100) {
            throw new Error("accepted global rate limit exceeded");
          }
          next.set(mapKey, count);
        } else if (statement.sql.startsWith("UPDATE rate_limits")) {
          const [, rateKey, day] = statement.args;
          const mapKey = `${day}:${rateKey}`;
          next.set(mapKey, Math.max((next.get(mapKey) || 0) - 1, 0));
        } else {
          throw new Error("unexpected batch statement");
        }
      }
      rates.clear();
      for (const [key, value] of next) rates.set(key, value);
      return statements.map(() => ({ success: true }));
    },
  };
  const BUNDLES = {
    async put(key, bytes) {
      if (putError) throw putError;
      if (beforePut) await beforePut();
      objects.set(key, Buffer.from(bytes));
    },
    async delete(key) {
      deleted.push(key);
      if (deleteError) throw deleteError;
      objects.delete(key);
    },
    async get(key) {
      const bytes = objects.get(key);
      if (!bytes) return null;
      return {
        body: bytes,
        async arrayBuffer() {
          return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        },
      };
    },
  };
  return {
    env: baseEnv({ DB, BUNDLES }),
    state: { rates, cases, objects, cleanups, deleted },
  };
}


function feedbackRequestForBundle(caseId, bundle = feedbackZip(caseId)) {
  return new Request("https://edge.example/v1/feedback", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "cf-connecting-ip": "192.0.2.10",
    },
    body: JSON.stringify({
      schema: "peerbridge.feedback-upload.v1",
      case_id: caseId,
      bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
      bundle_base64: bundle.toString("base64"),
    }),
  });
}


function feedbackRequest(caseId = "0123456789abcdef0123456789abcdef") {
  return feedbackRequestForBundle(caseId);
}


test("feedback upload validator derives bounded metadata only from the sealed report", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const bundle = feedbackZip(caseId);
  const payload = await testing.validateFeedbackUpload({
    schema: "peerbridge.feedback-upload.v1",
    case_id: caseId,
    bundle_sha256: "a".repeat(64),
    bundle_base64: bundle.toString("base64"),
  });
  assert.equal(payload.caseId, caseId);
  assert.equal(payload.summary, "Synthetic parser failure");
  assert.equal(payload.replyEmail, "reporter@example.com");
  assert.equal(payload.appVersion, "0.1.0");
  assert.deepEqual(Buffer.from(payload.bundleBytes), bundle);
  await assert.rejects(
    testing.validateFeedbackUpload({
      schema: "peerbridge.feedback-upload.v1",
      case_id: caseId,
      bundle_sha256: "a".repeat(64),
      bundle_base64: bundle.toString("base64"),
      summary: "unbound outer field",
    }),
    /fields are invalid/u,
  );
});


test("feedback upload rejects bytes that only imitate a ZIP signature", async () => {
  const fake = Buffer.from("PK synthetic but not a ZIP archive");
  await assert.rejects(
    testing.validateFeedbackUpload({
      schema: "peerbridge.feedback-upload.v1",
      case_id: "0123456789abcdef0123456789abcdef",
      bundle_sha256: createHash("sha256").update(fake).digest("hex"),
      bundle_base64: fake.toString("base64"),
    }),
    /ZIP/u,
  );
});


test("feedback ZIP rejects extra executable, duplicate, and unmanifested members", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  for (const bundle of [
    feedbackZip(caseId, { extras: [{ name: "payload.exe", payload: "MZ" }] }),
    feedbackZip(caseId, { extras: [{ name: "report.json", payload: "{}" }] }),
    feedbackZip(caseId, {
      extras: [{ name: "attachments/01.txt", payload: "unmanifested" }],
    }),
  ]) {
    await assert.rejects(
      testing.validateFeedbackUpload({
        schema: "peerbridge.feedback-upload.v1",
        case_id: caseId,
        bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
        bundle_base64: bundle.toString("base64"),
      }),
      /ZIP|manifest/u,
    );
  }
});


test("feedback ZIP rejects attachments whose bytes contradict their extension", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const disguisedImage = feedbackZip(caseId, {
    attachments: [{ name: "attachments/01.png", payload: "MZ-not-a-png" }],
  });
  const invalidJson = feedbackZip(caseId, {
    attachments: [{ name: "attachments/01.json", payload: "{not-json}" }],
  });

  for (const bundle of [disguisedImage, invalidJson]) {
    await assert.rejects(
      testing.validateFeedbackUpload({
        schema: "peerbridge.feedback-upload.v1",
        case_id: caseId,
        bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
        bundle_base64: bundle.toString("base64"),
      }),
      /attachment manifest does not match/u,
    );
  }
});


test("image attachment validation requires complete bounded structures", () => {
  for (const [name, payload] of VALID_IMAGES) {
    assert.equal(testing.attachmentPayloadIsValid(name, payload), true, name);
    assert.equal(testing.attachmentPayloadIsValid(name, payload.subarray(0, -1)), false, `${name} truncated`);
    assert.equal(
      testing.attachmentPayloadIsValid(name, Buffer.concat([payload, Buffer.from("polyglot")])),
      false,
      `${name} appended`,
    );
  }
});


test("image attachment validation rejects oversized PNG dimensions with a valid CRC", () => {
  const payload = Buffer.from(VALID_IMAGES.get("attachments/01.png"));
  payload.writeUInt32BE(32768, 16);
  payload.writeUInt32BE(32768, 20);
  payload.writeUInt32BE(crc32(payload.subarray(12, 29)), 29);
  assert.equal(testing.attachmentPayloadIsValid("attachments/01.png", payload), false);
});


test("feedback ZIP accepts a structurally complete image attachment", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const bundle = feedbackZip(caseId, {
    attachments: [{ name: "attachments/01.png", payload: VALID_IMAGES.get("attachments/01.png") }],
  });
  const result = await testing.validateFeedbackUpload({
    schema: "peerbridge.feedback-upload.v1",
    case_id: caseId,
    bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
    bundle_base64: bundle.toString("base64"),
  });
  assert.equal(result.caseId, caseId);
  assert.equal(result.bundleBytes.length, bundle.length);
});


test("feedback ZIP strictly validates the encrypted credential envelope", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const invalidEnvelopes = [
    validEncryptedEnvelope(caseId, { unexpected: "field" }),
    validEncryptedEnvelope("f".repeat(32)),
    validEncryptedEnvelope(caseId, {
      associated_data_b64: Buffer.from("wrong case binding", "ascii").toString("base64"),
    }),
    validEncryptedEnvelope(caseId, {
      wrapped_key_b64: Buffer.alloc(32, 0x31).toString("base64"),
    }),
    validEncryptedEnvelope(caseId, {
      nonce_b64: Buffer.alloc(11, 0x32).toString("base64"),
    }),
    validEncryptedEnvelope(caseId, {
      ciphertext_b64: Buffer.alloc(16, 0x33).toString("base64"),
    }),
  ];
  for (const encryptedEnvelope of invalidEnvelopes) {
    const bundle = feedbackZip(caseId, { encrypted: true, encryptedEnvelope });
    await assert.rejects(
      testing.validateFeedbackUpload({
        schema: "peerbridge.feedback-upload.v1",
        case_id: caseId,
        bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
        bundle_base64: bundle.toString("base64"),
      }),
      /encrypted credential/u,
    );
  }
});


test("feedback ZIP verifies decompressed bytes against CRC", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const bundle = Buffer.from(feedbackZip(caseId));
  const centralOffset = bundle.readUInt32LE(bundle.length - 6);
  const wrongCrc = (bundle.readUInt32LE(14) ^ 0xffffffff) >>> 0;
  bundle.writeUInt32LE(wrongCrc, 14);
  bundle.writeUInt32LE(wrongCrc, centralOffset + 16);

  await assert.rejects(
    testing.validateFeedbackUpload({
      schema: "peerbridge.feedback-upload.v1",
      case_id: caseId,
      bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
      bundle_base64: bundle.toString("base64"),
    }),
    /CRC/u,
  );
});


test("feedback ZIP aborts streaming expansion when declared size is forged", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const bundle = zipEntries([{
    name: "report.json",
    payload: Buffer.alloc(8 * 1024 * 1024, 0x41),
  }]);
  const forged = Buffer.from(bundle);
  const centralOffset = forged.readUInt32LE(forged.length - 6);
  forged.writeUInt32LE(1, 22);
  forged.writeUInt32LE(1, centralOffset + 24);

  await assert.rejects(
    testing.validateFeedbackUpload({
      schema: "peerbridge.feedback-upload.v1",
      case_id: caseId,
      bundle_sha256: createHash("sha256").update(forged).digest("hex"),
      bundle_base64: forged.toString("base64"),
    }),
    /expansion is too large/u,
  );
});


test("feedback ZIP rejects oversized and extreme-ratio members before expansion", async () => {
  const caseId = "0123456789abcdef0123456789abcdef";
  const oversizedReport = zipEntries([{
    name: "report.json",
    payload: Buffer.alloc(512 * 1024, 0x41),
  }]);
  await assert.rejects(
    testing.validateFeedbackUpload({
      schema: "peerbridge.feedback-upload.v1",
      case_id: caseId,
      bundle_sha256: createHash("sha256").update(oversizedReport).digest("hex"),
      bundle_base64: oversizedReport.toString("base64"),
    }),
    /member size is invalid/u,
  );

  const compressedBomb = feedbackZip(caseId, {
    attachments: [{
      name: "attachments/01.txt",
      payload: Buffer.alloc(1024 * 1024, 0x41),
    }],
  });
  await assert.rejects(
    testing.validateFeedbackUpload({
      schema: "peerbridge.feedback-upload.v1",
      case_id: caseId,
      bundle_sha256: createHash("sha256").update(compressedBomb).digest("hex"),
      bundle_base64: compressedBomb.toString("base64"),
    }),
    /compression ratio is unsafe/u,
  );
});


test("JSON reader enforces the actual streamed byte limit without Content-Length", async () => {
  let cancelled = false;
  const request = new Request("https://edge.example/v1/feedback", {
    method: "POST",
    duplex: "half",
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"a":"'));
        controller.enqueue(new Uint8Array(64));
      },
      cancel() {
        cancelled = true;
      },
    }),
  });
  await assert.rejects(testing.readJsonBody(request, 16), /too large/u);
  assert.equal(cancelled, true);
});


test("feedback upload derives contact from the sealed report and rejects malformed base64", async () => {
  const bundle = feedbackZip("0123456789abcdef0123456789abcdef");
  const common = {
    schema: "peerbridge.feedback-upload.v1",
    case_id: "0123456789abcdef0123456789abcdef",
    bundle_sha256: "a".repeat(64),
  };
  const upload = await testing.validateFeedbackUpload({
    ...common,
    bundle_base64: bundle.toString("base64"),
  });
  assert.equal(upload.replyEmail, "reporter@example.com");
  await assert.rejects(
    testing.validateFeedbackUpload({
      ...common,
      bundle_base64: "not base64",
    }),
    /base64/u,
  );
});


test("new feedback sends a metadata-only notification to the configured support inbox", async () => {
  const sent = [];
  const { env, state } = memoryIntakeEnv();
  env.FEEDBACK_EMAIL_FROM = "feedback@example.com";
  env.SUPPORT_DESTINATION_EMAIL = "maintainer@example.com";
  env.FEEDBACK_EMAIL = {
    async send(message) {
      sent.push(message);
      return { messageId: "email-test-1" };
    },
  };
  const response = await handleRequest(
    feedbackRequestForBundle(
      "1123456789abcdef0123456789abcdef",
      feedbackZip("1123456789abcdef0123456789abcdef", { encrypted: true }),
    ),
    env,
  );
  const receipt = await response.json();

  assert.equal(response.status, 201);
  assert.equal(receipt.notification_sent, true);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, "maintainer@example.com");
  assert.equal(sent[0].from, "feedback@example.com");
  assert.equal(sent[0].replyTo, "reporter@example.com");
  assert.match(sent[0].subject, /1123456789abcdef0123456789abcdef/u);
  assert.match(sent[0].text, /Reply address: reporter@example\.com/u);
  assert.match(sent[0].text, /Open the authenticated PeerBridge admin inbox/u);
  assert.doesNotMatch(sent[0].text, /Synthetic parser failure/u);
  assert.doesNotMatch(sent[0].text, /0\.1\.0/u);
  assert.doesNotMatch(JSON.stringify(sent[0]), /sk-test-do-not-email/u);
  assert.doesNotMatch(JSON.stringify(sent[0]), /Synthetic bounded report/u);
  const stored = [...state.cases.values()][0];
  assert.equal(stored.summary, "Private PeerBridge feedback bundle");
  assert.notEqual(stored.summary, "Synthetic parser failure");
});


test("Apps Script forwarding is HMAC-bound to a verified private bundle", async () => {
  const secret = "test-apps-script-secret-0123456789-ABCDEFGHIJKLMN";
  const caseId = "3123456789abcdef0123456789abcdef";
  const bundle = feedbackZip(caseId);
  const upload = await testing.validateFeedbackUpload({
    schema: "peerbridge.feedback-upload.v1",
    case_id: caseId,
    bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
    bundle_base64: bundle.toString("base64"),
  });
  const originalFetch = globalThis.fetch;
  let observedUrl;
  let observedInit;
  globalThis.fetch = async (url, init) => {
    observedUrl = String(url);
    observedInit = init;
    return new Response(JSON.stringify({
      ok: true,
      case_id: upload.caseId,
      bundle_sha256: upload.expectedSha,
      receipt: "emailed_to_private_support",
    }), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    assert.equal(await testing.sendAppsScriptNotification({
      GOOGLE_APPS_SCRIPT_URL: "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec",
      GOOGLE_APPS_SCRIPT_SECRET: secret,
    }, upload, "2026-08-17T00:00:00.000Z"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
  const body = JSON.parse(observedInit.body);
  const expectedSignature = createHmac("sha256", secret).update([
    body.schema,
    body.case_id,
    body.bundle_sha256,
    body.ingress_timestamp,
    body.ingress_nonce,
    body.reply_email,
    body.received_utc,
  ].join("\n")).digest("hex");
  assert.equal(observedUrl, "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec");
  assert.equal(observedInit.redirect, "manual");
  assert.equal(body.ingress_signature, expectedSignature);
  assert.equal(body.bundle_base64, bundle.toString("base64"));
  assert.equal(Object.hasOwn(body, "attachments"), false);
  assert.equal(Object.hasOwn(body, "diagnostics"), false);
  assert.equal(Object.hasOwn(body, "summary"), false);
  assert.equal(Object.hasOwn(body, "app_version"), false);
  assert.doesNotMatch(JSON.stringify(observedInit), new RegExp(secret, "u"));
  assert.ok(Buffer.byteLength(observedInit.body, "utf8") < 16 * 1024);
});


test("Apps Script follows only a trusted receipt redirect without forwarding the bundle", async () => {
  const secret = "test-apps-script-secret-0123456789-ABCDEFGHIJKLMN";
  const caseId = "7123456789abcdef0123456789abcdef";
  const bundle = feedbackZip(caseId);
  const upload = await testing.validateFeedbackUpload({
    schema: "peerbridge.feedback-upload.v1",
    case_id: caseId,
    bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
    bundle_base64: bundle.toString("base64"),
  });
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    if (calls.length === 1) {
      return new Response(null, {
        status: 302,
        headers: { location: "https://script.googleusercontent.com/macros/echo?receipt=accepted" },
      });
    }
    return new Response(JSON.stringify({
      ok: true,
      case_id: upload.caseId,
      bundle_sha256: upload.expectedSha,
    }), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    assert.equal(await testing.sendAppsScriptNotification({
      GOOGLE_APPS_SCRIPT_URL: "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec",
      GOOGLE_APPS_SCRIPT_SECRET: secret,
    }, upload, "2026-08-17T00:00:00.000Z"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.redirect, "manual");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(Object.hasOwn(calls[1].init, "body"), false);
  assert.equal(JSON.stringify(calls[1]).includes(bundle.toString("base64")), false);
});


test("Apps Script rejects an untrusted receipt redirect", async () => {
  const caseId = "8123456789abcdef0123456789abcdef";
  const bundle = feedbackZip(caseId);
  const upload = await testing.validateFeedbackUpload({
    schema: "peerbridge.feedback-upload.v1",
    case_id: caseId,
    bundle_sha256: createHash("sha256").update(bundle).digest("hex"),
    bundle_base64: bundle.toString("base64"),
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, {
    status: 302,
    headers: { location: "https://attacker.example/collect" },
  });
  try {
    await assert.rejects(
      testing.sendAppsScriptNotification({
        GOOGLE_APPS_SCRIPT_URL: "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec",
        GOOGLE_APPS_SCRIPT_SECRET: "test-apps-script-secret-0123456789-ABCDEFGHIJKLMN",
      }, upload, "2026-08-17T00:00:00.000Z"),
      /redirect is not trusted/u,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("R2-free feedback is delivered by Apps Script and recorded as email-only", async () => {
  const { env, state } = memoryIntakeEnv();
  delete env.BUNDLES;
  env.GOOGLE_APPS_SCRIPT_URL = "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec";
  env.GOOGLE_APPS_SCRIPT_SECRET = "test-apps-script-secret-0123456789-ABCDEFGHIJKLMN";
  const originalFetch = globalThis.fetch;
  let forwarded = 0;
  globalThis.fetch = async (_url, init) => {
    forwarded += 1;
    const body = JSON.parse(init.body);
    return new Response(JSON.stringify({
      ok: true,
      case_id: body.case_id,
      bundle_sha256: body.bundle_sha256,
      receipt: "emailed_to_private_support",
    }), { status: 200, headers: { "content-type": "application/json" } });
  };
  let response;
  try {
    response = await handleRequest(
      feedbackRequest("4123456789abcdef0123456789abcdef"),
      env,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(response.status, 201);
  assert.equal((await response.json()).notification_sent, true);
  assert.equal(forwarded, 1);
  assert.equal(state.cases.size, 1);
  assert.equal(state.objects.size, 0);
  assert.match([...state.cases.values()][0].object_key, /^email:/u);
});


test("Apps Script endpoint rejects redirects, queries, and non-Google hosts", () => {
  assert.throws(
    () => testing.normalizeAppsScriptEndpoint(
      "https://script.google.com.evil.example/macros/s/DEPLOYMENT/exec",
    ),
    /deployed \/exec URL/u,
  );
  assert.throws(
    () => testing.normalizeAppsScriptEndpoint(
      "https://script.google.com/macros/s/DEPLOYMENT/exec?redirect=evil",
    ),
    /deployed \/exec URL/u,
  );
});


test("notification failure does not invalidate a committed feedback receipt", async () => {
  const { env, state } = memoryIntakeEnv();
  env.FEEDBACK_EMAIL_FROM = "feedback@example.com";
  env.SUPPORT_DESTINATION_EMAIL = "maintainer@example.com";
  env.FEEDBACK_EMAIL = { send: async () => { throw new Error("mail outage detail"); } };
  const originalError = console.error;
  const logs = [];
  console.error = (line) => logs.push(String(line));
  let response;
  try {
    response = await handleRequest(
      feedbackRequest("2123456789abcdef0123456789abcdef"),
      env,
    );
  } finally {
    console.error = originalError;
  }
  const receipt = await response.json();

  assert.equal(response.status, 201);
  assert.equal(receipt.notification_sent, false);
  assert.equal(state.cases.size, 1);
  assert.equal(state.objects.size, 1);
  assert.equal([...state.cases.values()][0].notification_status, "failed");
  assert.equal([...state.cases.values()][0].notification_attempt_count, 1);
  assert.equal(logs.length, 1);
  assert.doesNotMatch(logs[0], /mail outage detail/u);
});


test("scheduled notification retry reads the sealed R2 bundle and sends only once", async () => {
  const { env, state } = memoryIntakeEnv();
  env.GOOGLE_APPS_SCRIPT_URL = "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec";
  env.GOOGLE_APPS_SCRIPT_SECRET = "test-apps-script-secret-0123456789-ABCDEFGHIJKLMN";
  const originalFetch = globalThis.fetch;
  const originalError = console.error;
  let attempts = 0;
  let retriedBody = null;
  console.error = () => {};
  globalThis.fetch = async (_url, init) => {
    attempts += 1;
    if (attempts === 1) throw new Error("synthetic outage");
    retriedBody = JSON.parse(init.body);
    return new Response(JSON.stringify({
      ok: true,
      case_id: retriedBody.case_id,
      bundle_sha256: retriedBody.bundle_sha256,
    }), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    const response = await handleRequest(
      feedbackRequest("3123456789abcdef0123456789abcdef"),
      env,
    );
    assert.equal(response.status, 201);
    assert.equal((await response.json()).notification_sent, false);
    assert.equal([...state.cases.values()][0].notification_status, "failed");

    assert.equal(await testing.retryPendingFeedbackNotifications(env), 1);
    const row = [...state.cases.values()][0];
    assert.equal(row.notification_status, "sent");
    assert.equal(row.notification_attempt_count, 2);
    assert.equal(typeof row.notification_sent_utc, "string");
    assert.equal(
      createHash("sha256").update(Buffer.from(retriedBody.bundle_base64, "base64")).digest("hex"),
      row.bundle_sha256,
    );

    assert.equal(await testing.retryPendingFeedbackNotifications(env), 0);
    assert.equal(attempts, 2);
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalError;
  }
});


test("concurrent notification paths atomically claim one delivery", async () => {
  const { env, state } = memoryIntakeEnv();
  env.FEEDBACK_EMAIL_FROM = "feedback@example.com";
  env.SUPPORT_DESTINATION_EMAIL = "maintainer@example.com";
  env.FEEDBACK_EMAIL = { send: async () => { throw new Error("initial outage"); } };
  const caseId = "7123456789abcdef0123456789abcdef";
  const originalError = console.error;
  console.error = () => {};
  try {
    const response = await handleRequest(feedbackRequest(caseId), env);
    assert.equal(response.status, 201);
    const row = state.cases.get(caseId);
    assert.equal(row.notification_status, "failed");
    assert.equal(row.notification_attempt_count, 1);

    let releaseSend;
    let signalStarted;
    const sendGate = new Promise((resolve) => { releaseSend = resolve; });
    const sendStarted = new Promise((resolve) => { signalStarted = resolve; });
    let sends = 0;
    env.FEEDBACK_EMAIL.send = async () => {
      sends += 1;
      signalStarted();
      await sendGate;
    };
    const upload = {
      caseId,
      expectedSha: row.bundle_sha256,
      replyEmail: null,
      bundleBase64: "",
    };
    const first = testing.attemptFeedbackNotification(env, upload, row.received_utc);
    await sendStarted;
    const second = await testing.attemptFeedbackNotification(env, upload, row.received_utc);
    assert.equal(second, false);
    releaseSend();
    assert.equal(await first, true);
    assert.equal(sends, 1);
    assert.equal(row.notification_status, "sent");
    assert.equal(row.notification_attempt_count, 2);
    assert.equal(row.notification_claim_token_sha256, null);
    assert.equal(row.notification_claim_expires_utc, null);
  } finally {
    console.error = originalError;
  }
});


test("feedback endpoint rejects a false bundle receipt before storage", async () => {
  const { env, state } = memoryIntakeEnv();
  const response = await handleRequest(new Request("https://edge.example/v1/feedback", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      schema: "peerbridge.feedback-upload.v1",
      case_id: "0123456789abcdef0123456789abcdef",
      bundle_sha256: "0".repeat(64),
      bundle_base64: feedbackZip("0123456789abcdef0123456789abcdef").toString("base64"),
    }),
  }), env);
  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.equal(payload.error, "bundle_sha256_mismatch");
  assert.equal(Math.max(...state.rates.values()), 1);
  assert.equal(state.objects.size, 0);
});


test("malformed feedback consumes its abuse-resistant attempt quota", async () => {
  const { env, state } = memoryIntakeEnv();
  const response = await handleRequest(new Request("https://edge.example/v1/feedback", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "cf-connecting-ip": "192.0.2.11",
    },
    body: "{not-json",
  }), env);

  assert.equal(response.status, 400);
  assert.equal((await response.json()).error, "invalid_feedback");
  assert.equal(Math.max(...state.rates.values()), 1);
});


test("malformed traffic cannot consume the accepted global case quota", async () => {
  const { env, state } = memoryIntakeEnv();
  for (let index = 0; index < 101; index += 1) {
    const response = await handleRequest(new Request("https://edge.example/v1/feedback", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "cf-connecting-ip": `192.0.2.${index + 1}`,
      },
      body: "{not-json",
    }), env);
    assert.equal(response.status, 400);
  }
  assert.equal(
    [...state.rates].some(([key]) => key.includes("accepted-global")),
    false,
  );
  const accepted = await handleRequest(feedbackRequest(), env);
  assert.equal(accepted.status, 201);
});


test("distributed malformed traffic hits a global attempt cap before body parsing", async () => {
  const { env } = memoryIntakeEnv();
  for (let index = 0; index < 500; index += 1) {
    const result = await testing.rateLimit(
      new Request("https://edge.example/v1/feedback", {
        headers: { "cf-connecting-ip": `198.51.100.${index}` },
      }),
      env,
      "2026-08-17T00:00:00.000Z",
      "attempt",
    );
    assert.equal(result.response, null);
  }
  let bodyRead = false;
  const blockedRequest = {
    url: "https://edge.example/v1/feedback",
    method: "POST",
    headers: new Headers({
      "content-type": "application/json",
      "cf-connecting-ip": "203.0.113.250",
    }),
    get body() {
      bodyRead = true;
      throw new Error("body must not be read after the global attempt cap");
    },
  };
  const response = await handleRequest(blockedRequest, env);
  assert.equal(response.status, 429);
  assert.equal((await response.json()).error, "service_attempt_rate_limited");
  assert.equal(bodyRead, false);
});


test("duplicate and conflicting feedback also consume attempt quota", async () => {
  const { env, state } = memoryIntakeEnv();
  const caseId = "0123456789abcdef0123456789abcdef";
  const first = await handleRequest(feedbackRequest(caseId), env);
  assert.equal(first.status, 201);

  const duplicate = await handleRequest(feedbackRequest(caseId), env);
  assert.equal(duplicate.status, 200);
  assert.equal((await duplicate.json()).duplicate, true);

  const conflict = await handleRequest(
    feedbackRequestForBundle(caseId, feedbackZip(caseId, { encrypted: true })),
    env,
  );
  assert.equal(conflict.status, 409);
  assert.equal((await conflict.json()).error, "case_conflict");
  assert.equal(Math.max(...state.rates.values()), 3);
  assert.equal(state.objects.size, 1);
});


test("admin inbox rejects missing authorization without querying storage", async () => {
  const response = await handleRequest(
    new Request("https://edge.example/v1/admin/feedback"),
    baseEnv(),
  );
  assert.equal(response.status, 401);
  assert.equal((await response.json()).error, "unauthorized");
});


test("announcement validator is plain-text and HTTPS-only", () => {
  const announcement = testing.validateAnnouncement({
    announcement_id: "alpha-20260815",
    locale: "zh-Hant",
    title: "Alpha 公告",
    body: "這是一則不執行命令的測試公告。",
    severity: "important",
    link_url: "https://github.com/oscarho200407-hue/peerbridge-mcp/releases",
    published_utc: "2026-08-15T00:00:00Z",
  });
  assert.equal(announcement.locale, "zh-Hant");
  assert.equal(announcement.severity, "important");
  assert.throws(
    () => testing.validateAnnouncement({
      ...announcement,
      announcement_id: "bad-link",
      link_url: "javascript:alert(1)",
    }),
    /HTTPS/u,
  );
});


test("public announcement feed returns only database-projected fields", async () => {
  const rows = [{
    announcement_id: "alpha-20260815",
    locale: "en",
    title: "Alpha notice",
    body: "A bounded test notice.",
    severity: "info",
    link_url: null,
    published_utc: "2026-08-15T00:00:00.000Z",
    expires_utc: null,
  }];
  const DB = {
    prepare(sql) {
      assert.match(sql, /FROM announcements/u);
      return {
        bind() {
          return { all: async () => ({ results: rows }) };
        },
      };
    },
  };
  const response = await handleRequest(
    new Request("https://edge.example/v1/announcements?locale=en"),
    baseEnv({ DB }),
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.schema, "peerbridge.announcement-feed.v1");
  assert.deepEqual(payload.announcements, rows);
  assert.equal(response.headers.get("access-control-allow-origin"), null);
});


test("constant-time equality helper rejects length and content changes", () => {
  assert.equal(testing.timingSafeEqual("abc", "abc"), true);
  assert.equal(testing.timingSafeEqual("abc", "abd"), false);
  assert.equal(testing.timingSafeEqual("abc", "ab"), false);
});


test("accepted-case rate reservation is atomic and reversible", async () => {
  const { env, state } = memoryIntakeEnv();
  const request = new Request("https://edge.example/v1/feedback", {
    headers: { "cf-connecting-ip": "192.0.2.10" },
  });
  let lastReservation;
  for (let index = 0; index < 5; index += 1) {
    const result = await testing.rateLimit(request, env, "2026-08-15T00:00:00.000Z", "accepted");
    assert.equal(result.response, null);
    lastReservation = result.reservation;
  }
  const blocked = await testing.rateLimit(request, env, "2026-08-15T00:01:00.000Z", "accepted");
  assert.equal(blocked.response.status, 429);
  assert.equal((await blocked.response.json()).error, "source_rate_limited");
  assert.equal(Math.max(...state.rates.values()), 5);

  await testing.releaseRateLimit(env, lastReservation);
  assert.equal(Math.max(...state.rates.values()), 4);
});


test("concurrent identical replay keeps exactly one accepted reservation", async () => {
  let putCount = 0;
  let releasePuts;
  const bothPutsReached = new Promise((resolve) => { releasePuts = resolve; });
  const { env, state } = memoryIntakeEnv({
    async beforePut() {
      putCount += 1;
      if (putCount === 2) releasePuts();
      await bothPutsReached;
    },
  });

  const responses = await Promise.all([
    handleRequest(feedbackRequest(), env),
    handleRequest(feedbackRequest(), env),
  ]);
  assert.deepEqual(responses.map((response) => response.status).sort(), [200, 201]);
  const receipts = await Promise.all(responses.map((response) => response.json()));
  assert.equal(receipts.filter((receipt) => receipt.duplicate === false).length, 1);
  assert.equal(receipts.filter((receipt) => receipt.duplicate === true).length, 1);
  const acceptedCounts = [...state.rates]
    .filter(([key]) => key.includes(":accepted-"))
    .map(([, count]) => count);
  assert.deepEqual(acceptedCounts, [1, 1]);
  assert.equal(state.cases.size, 1);
  assert.equal(state.objects.size, 1);
});


test("feedback storage failure releases quota without leaking infrastructure detail", async () => {
  const sentinel = "private R2 bucket binding failed";
  const { env, state } = memoryIntakeEnv({ putError: new Error(sentinel) });
  const originalError = console.error;
  console.error = () => {};
  let response;
  try {
    response = await handleRequest(feedbackRequest(), env);
  } finally {
    console.error = originalError;
  }
  assert.equal(response.status, 503);
  const body = await response.text();
  assert.doesNotMatch(body, /private R2 bucket binding failed/u);
  assert.match(body, /Service temporarily unavailable/u);
  assert.equal(Math.max(...state.rates.values()), 1);
  assert.equal([...state.rates].find(([key]) => key.includes("accepted-global"))[1], 0);
  assert.equal(state.objects.size, 0);
});


test("metadata failure deletes the R2 object and releases quota", async () => {
  const { env, state } = memoryIntakeEnv({ insertError: new Error("D1 unavailable") });
  const originalError = console.error;
  console.error = () => {};
  let response;
  try {
    response = await handleRequest(feedbackRequest(), env);
  } finally {
    console.error = originalError;
  }
  assert.equal(response.status, 503);
  assert.equal(Math.max(...state.rates.values()), 1);
  assert.equal([...state.rates].find(([key]) => key.includes("accepted-global"))[1], 0);
  assert.equal(state.objects.size, 0);
  assert.equal(state.deleted.length, 1);
});


test("rollback delete failure creates a durable object cleanup record", async () => {
  const { env, state } = memoryIntakeEnv({
    insertError: new Error("D1 feedback insert unavailable"),
    deleteError: new Error("R2 delete unavailable"),
  });
  const originalError = console.error;
  console.error = () => {};
  let response;
  try {
    response = await handleRequest(feedbackRequest(), env);
  } finally {
    console.error = originalError;
  }

  assert.equal(response.status, 503);
  assert.equal(state.objects.size, 1);
  assert.equal(state.cleanups.size, 1);
  const cleanup = [...state.cleanups.values()][0];
  assert.equal(cleanup.reason, "intake_rollback_delete_failed");
  assert.match(cleanup.object_key, /^feedback\//u);
  assert.equal(cleanup.attempt_count, 0);
  assert.equal([...state.rates].find(([key]) => key.includes("accepted-global"))[1], 0);
});


test("ambiguous D1 acknowledgement preserves an already committed intake", async () => {
  const { env, state } = memoryIntakeEnv({ commitThenThrow: true });
  const response = await handleRequest(feedbackRequest(), env);
  const payload = await response.json();

  assert.equal(response.status, 201);
  assert.equal(payload.ok, true);
  assert.equal(payload.duplicate, false);
  assert.equal(Math.max(...state.rates.values()), 1);
  assert.equal(state.objects.size, 1);
  assert.equal(state.deleted.length, 0);
  assert.equal(state.cases.size, 1);
});


test("D1 schema enforces attempt and accepted-case rate caps", () => {
  const schema = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");
  assert.match(schema, /submission_id TEXT NOT NULL UNIQUE/u);
  assert.match(schema, /rate_limits_attempt_source_cap_update/u);
  assert.match(schema, /NEW\.request_count > 20/u);
  assert.match(schema, /rate_limits_attempt_global_cap_update/u);
  assert.match(schema, /NEW\.rate_key = 'attempt-global' AND NEW\.request_count > 500/u);
  assert.match(schema, /rate_limits_accepted_source_cap_update/u);
  assert.match(schema, /NEW\.request_count > 5/u);
  assert.match(schema, /rate_limits_accepted_global_cap_update/u);
  assert.match(schema, /NEW\.request_count > 100/u);
  assert.match(schema, /expires_utc TEXT NOT NULL/u);
  const cleanupMigration = readFileSync(
    new URL("../migrations/0006_feedback_object_cleanup.sql", import.meta.url),
    "utf8",
  );
  assert.match(cleanupMigration, /CREATE TABLE IF NOT EXISTS feedback_object_cleanup/u);
  assert.match(cleanupMigration, /object_key TEXT PRIMARY KEY/u);
});


test("retention pruning deletes the sealed object before its metadata row", async () => {
  const events = [];
  const row = {
    case_id: "0123456789abcdef0123456789abcdef",
    object_key: "feedback/0123456789abcdef0123456789abcdef/bundle.zip",
  };
  const env = baseEnv({
    BUNDLES: {
      async delete(key) {
        events.push(`object:${key}`);
      },
    },
    DB: {
      prepare(sql) {
        return {
          bind(...args) {
            if (sql.startsWith("SELECT case_id, object_key")) {
              return { all: async () => ({ results: [row] }) };
            }
            if (sql.startsWith("DELETE FROM feedback_cases")) {
              return {
                async run() {
                  events.push(`row:${args[0]}`);
                  return { meta: { changes: 1 } };
                },
              };
            }
            throw new Error("unexpected retention query");
          },
        };
      },
    },
  });
  const removed = await testing.pruneExpiredFeedback(
    env,
    "2026-09-17T00:00:00.000Z",
  );
  assert.equal(removed, 1);
  assert.deepEqual(events, [
    `object:${row.object_key}`,
    `row:${row.case_id}`,
  ]);
});


test("retention pruning keeps R2-backed metadata when the binding is missing", async () => {
  let metadataDeleted = false;
  const env = baseEnv({
    BUNDLES: undefined,
    DB: {
      prepare(sql) {
        return {
          bind() {
            if (sql.startsWith("SELECT case_id, object_key")) {
              return {
                all: async () => ({
                  results: [{
                    case_id: "0123456789abcdef0123456789abcdef",
                    object_key: "feedback/0123456789abcdef0123456789abcdef/bundle.zip",
                  }],
                }),
              };
            }
            if (sql.startsWith("DELETE FROM feedback_cases")) {
              return { run: async () => { metadataDeleted = true; } };
            }
            throw new Error("unexpected retention query");
          },
        };
      },
    },
  });
  const originalError = console.error;
  console.error = () => {};
  let removed;
  try {
    removed = await testing.pruneExpiredFeedback(env, "2026-09-17T00:00:00.000Z");
  } finally {
    console.error = originalError;
  }
  assert.equal(removed, 0);
  assert.equal(metadataDeleted, false);
});


test("scheduled orphan cleanup deletes R2 before its durable record", async () => {
  const events = [];
  const row = {
    case_id: "0123456789abcdef0123456789abcdef",
    object_key: "feedback/0123456789abcdef0123456789abcdef/orphan.zip",
  };
  const env = baseEnv({
    BUNDLES: {
      async delete(key) {
        events.push(`object:${key}`);
      },
    },
    DB: {
      prepare(sql) {
        return {
          bind(...args) {
            if (sql.startsWith("SELECT object_key, case_id FROM feedback_object_cleanup")) {
              return { all: async () => ({ results: [row] }) };
            }
            if (sql.startsWith("SELECT case_id FROM feedback_cases WHERE object_key")) {
              return { first: async () => null };
            }
            if (sql.startsWith("DELETE FROM feedback_object_cleanup")) {
              return {
                async run() {
                  events.push(`row:${args[0]}`);
                  return { meta: { changes: 1 } };
                },
              };
            }
            throw new Error("unexpected object cleanup query");
          },
        };
      },
    },
  });
  const removed = await testing.pruneOrphanedFeedbackObjects(
    env,
    "2026-08-17T00:00:00.000Z",
  );
  assert.equal(removed, 1);
  assert.deepEqual(events, [
    `object:${row.object_key}`,
    `row:${row.object_key}`,
  ]);
});
