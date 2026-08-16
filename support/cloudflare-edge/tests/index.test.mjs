import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";

import { handleRequest, testing } from "../src/index.js";


function baseEnv(overrides = {}) {
  return {
    DB: {},
    BUNDLES: {},
    RATE_SALT: "test-rate-salt-not-for-production",
    ADMIN_TOKEN: "test-admin-token-not-for-production",
    ...overrides,
  };
}


function memoryIntakeEnv({ putError = null, insertError = null, commitThenThrow = false } = {}) {
  const rates = new Map();
  const cases = new Map();
  const objects = new Map();
  const deleted = [];
  const DB = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            sql,
            args,
            async all() {
              if (!sql.includes("FROM rate_limits")) throw new Error("unexpected all query");
              const [day, sourceKey, globalKey] = args;
              return {
                results: [sourceKey, globalKey]
                  .map((rateKey) => ({
                    rate_key: rateKey,
                    request_count: rates.get(`${day}:${rateKey}`) || 0,
                  })),
              };
            },
            async first() {
              if (!sql.includes("FROM feedback_cases")) throw new Error("unexpected first query");
              return cases.get(args[0]) || null;
            },
            async run() {
              if (!sql.includes("INSERT INTO feedback_cases")) {
                throw new Error("unexpected run query");
              }
              const row = {
                case_id: args[0],
                bundle_sha256: args[1],
                submission_id: args[2],
                received_utc: args[8],
              };
              if (commitThenThrow) {
                cases.set(args[0], row);
                throw new Error("ambiguous D1 acknowledgement");
              }
              if (insertError) throw insertError;
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
          if (rateKey.startsWith("source:") && count > 50) {
            throw new Error("source rate limit exceeded");
          }
          if (rateKey === "global" && count > 5000) {
            throw new Error("global rate limit exceeded");
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
      objects.set(key, Buffer.from(bytes));
    },
    async delete(key) {
      deleted.push(key);
      objects.delete(key);
    },
  };
  return {
    env: baseEnv({ DB, BUNDLES }),
    state: { rates, cases, objects, deleted },
  };
}


function feedbackRequestForBundle(caseId, bundle) {
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
      summary: "Synthetic parser failure",
      reply_email: "reporter@example.com",
      app_version: "0.1.0",
    }),
  });
}


function feedbackRequest(caseId = "0123456789abcdef0123456789abcdef") {
  return feedbackRequestForBundle(
    caseId,
    Buffer.from("PK deterministic encrypted feedback fixture"),
  );
}


test("feedback upload validator preserves bounded reply metadata", () => {
  const payload = testing.validateFeedbackUpload({
    schema: "peerbridge.feedback-upload.v1",
    case_id: "0123456789abcdef0123456789abcdef",
    bundle_sha256: "a".repeat(64),
    bundle_base64: Buffer.from("PK synthetic bundle").toString("base64"),
    summary: "Synthetic parser failure",
    reply_email: "reporter@example.com",
    app_version: "0.1.0",
  });
  assert.equal(payload.caseId, "0123456789abcdef0123456789abcdef");
  assert.equal(payload.replyEmail, "reporter@example.com");
  assert.equal(Buffer.from(payload.bundleBytes).toString(), "PK synthetic bundle");
});


test("feedback upload rejects malformed email and base64", () => {
  const common = {
    schema: "peerbridge.feedback-upload.v1",
    case_id: "0123456789abcdef0123456789abcdef",
    bundle_sha256: "a".repeat(64),
    summary: "Synthetic parser failure",
  };
  assert.throws(
    () => testing.validateFeedbackUpload({
      ...common,
      bundle_base64: Buffer.from("bundle").toString("base64"),
      reply_email: "not-an-email",
    }),
    /reply email is invalid/u,
  );
  assert.throws(
    () => testing.validateFeedbackUpload({
      ...common,
      bundle_base64: "not base64",
    }),
    /base64/u,
  );
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
      bundle_base64: Buffer.from("PK synthetic bundle").toString("base64"),
      summary: "Synthetic parser failure",
    }),
  }), env);
  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.equal(payload.error, "bundle_sha256_mismatch");
  assert.equal(Math.max(...state.rates.values()), 1);
  assert.equal(state.objects.size, 0);
});


test("malformed feedback consumes quota before JSON parsing", async () => {
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


test("duplicate and conflicting feedback both consume quota", async () => {
  const { env, state } = memoryIntakeEnv();
  const caseId = "0123456789abcdef0123456789abcdef";
  const first = await handleRequest(feedbackRequest(caseId), env);
  assert.equal(first.status, 201);

  const duplicate = await handleRequest(feedbackRequest(caseId), env);
  assert.equal(duplicate.status, 200);
  assert.equal((await duplicate.json()).duplicate, true);

  const conflict = await handleRequest(
    feedbackRequestForBundle(caseId, Buffer.from("PK different encrypted feedback fixture")),
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


test("rate limit reservation is atomic and reversible", async () => {
  const { env, state } = memoryIntakeEnv();
  const request = new Request("https://edge.example/v1/feedback", {
    headers: { "cf-connecting-ip": "192.0.2.10" },
  });
  let lastReservation;
  for (let index = 0; index < 50; index += 1) {
    const result = await testing.rateLimit(request, env, "2026-08-15T00:00:00.000Z");
    assert.equal(result.response, null);
    lastReservation = result.reservation;
  }
  const blocked = await testing.rateLimit(request, env, "2026-08-15T00:01:00.000Z");
  assert.equal(blocked.response.status, 429);
  assert.equal((await blocked.response.json()).error, "source_rate_limited");
  assert.equal(Math.max(...state.rates.values()), 50);

  await testing.releaseRateLimit(env, lastReservation);
  assert.equal(Math.max(...state.rates.values()), 49);
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
  assert.equal(Math.max(...state.rates.values()), 0);
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
  assert.equal(Math.max(...state.rates.values()), 0);
  assert.equal(state.objects.size, 0);
  assert.equal(state.deleted.length, 1);
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


test("D1 schema enforces concurrent source and global rate caps", () => {
  const schema = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");
  assert.match(schema, /submission_id TEXT NOT NULL UNIQUE/u);
  assert.match(schema, /rate_limits_source_cap_update/u);
  assert.match(schema, /NEW\.request_count > 50/u);
  assert.match(schema, /rate_limits_global_cap_update/u);
  assert.match(schema, /NEW\.request_count > 5000/u);
});
