const FEEDBACK_SCHEMA = "peerbridge.feedback-upload.v1";
const ANNOUNCEMENT_SCHEMA = "peerbridge.announcement-feed.v1";
const DIGEST_SCHEMA = "peerbridge.feedback-digest.v1";

const MAX_UPLOAD_JSON_BYTES = 24 * 1024 * 1024;
const MAX_BUNDLE_BYTES = 16 * 1024 * 1024;
const MAX_ADMIN_RESPONSE_CASES = 100;
const MAX_PER_SOURCE_PER_DAY = 50;
const MAX_GLOBAL_PER_DAY = 5000;
const ALLOWED_LOCALES = new Set(["en", "zh-Hans", "zh-Hant"]);
const ALLOWED_SEVERITIES = new Set(["info", "important", "critical"]);
const ALLOWED_CASE_STATUSES = new Set(["new", "read", "replied", "closed"]);

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...headers,
    },
  });
}

function errorResponse(code, status, message) {
  return jsonResponse({ ok: false, error: code, message }, status);
}

function requireBindings(env) {
  if (!env?.DB || !env?.BUNDLES || !env?.RATE_SALT || !env?.ADMIN_TOKEN) {
    throw new Error("required Worker bindings or secrets are unavailable");
  }
}

function cleanText(value, maximum, label, { required = false } = {}) {
  const text = typeof value === "string" ? value.trim() : "";
  if (required && !text) {
    throw new TypeError(`${label} is required`);
  }
  if (text.length > maximum || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(text)) {
    throw new TypeError(`${label} is invalid`);
  }
  return text;
}

function normalizeEmail(value) {
  const text = cleanText(value, 320, "reply email");
  if (!text) return null;
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/u.test(text)) {
    throw new TypeError("reply email is invalid");
  }
  return text;
}

function normalizeHttpsUrl(value) {
  const text = cleanText(value, 2048, "HTTPS link");
  if (!text) return null;
  let parsed;
  try {
    parsed = new URL(text);
  } catch {
    throw new TypeError("announcement link is invalid");
  }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.hash) {
    throw new TypeError("announcement link must be a plain HTTPS URL");
  }
  return parsed.toString();
}

function decodeBase64(value) {
  if (typeof value !== "string" || !value || value.length > Math.ceil(MAX_BUNDLE_BYTES / 3) * 4 + 8) {
    throw new TypeError("feedback bundle is invalid");
  }
  if (!/^[A-Za-z0-9+/]*={0,2}$/u.test(value) || value.length % 4 !== 0) {
    throw new TypeError("feedback bundle is not canonical base64");
  }
  let binary;
  try {
    binary = atob(value);
  } catch {
    throw new TypeError("feedback bundle is not valid base64");
  }
  if (!binary.length || binary.length > MAX_BUNDLE_BYTES) {
    throw new TypeError("feedback bundle size is invalid");
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(bytes) {
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

async function hmacHex(secret, text) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return hex(await crypto.subtle.sign("HMAC", key, encoder.encode(text)));
}

function timingSafeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

async function readJsonBody(request, maximumBytes) {
  const declared = Number(request.headers.get("content-length") || "0");
  if (declared > maximumBytes) throw new TypeError("request body is too large");
  const bytes = await request.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > maximumBytes) {
    throw new TypeError("request body size is invalid");
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new TypeError("request body is not valid UTF-8 JSON");
  }
}

async function rateLimit(request, env, now) {
  const day = now.slice(0, 10);
  const source = request.headers.get("cf-connecting-ip") || "unknown";
  const sourceKey = `source:${await hmacHex(env.RATE_SALT, source)}`;
  const globalKey = "global";
  const statements = [sourceKey, globalKey].map((rateKey) => env.DB.prepare(
    "INSERT INTO rate_limits(rate_key, rate_day, request_count, updated_utc) VALUES (?, ?, 1, ?) " +
    "ON CONFLICT(rate_key, rate_day) DO UPDATE SET request_count = request_count + 1, updated_utc = excluded.updated_utc",
  ).bind(rateKey, day, now));
  try {
    await env.DB.batch(statements);
  } catch (error) {
    const rows = await env.DB.prepare(
      "SELECT rate_key, request_count FROM rate_limits WHERE rate_day = ? AND rate_key IN (?, ?)",
    ).bind(day, sourceKey, globalKey).all();
    const counts = new Map((rows.results || []).map(
      (row) => [row.rate_key, Number(row.request_count)],
    ));
    if ((counts.get(sourceKey) || 0) >= MAX_PER_SOURCE_PER_DAY) {
      return {
        response: errorResponse(
          "source_rate_limited",
          429,
          "Daily feedback limit reached for this source.",
        ),
        reservation: null,
      };
    }
    if ((counts.get(globalKey) || 0) >= MAX_GLOBAL_PER_DAY) {
      return {
        response: errorResponse(
          "service_rate_limited",
          429,
          "Daily feedback service limit reached.",
        ),
        reservation: null,
      };
    }
    throw error;
  }
  return {
    response: null,
    reservation: { day, sourceKey, globalKey, updatedUtc: now },
  };
}

async function releaseRateLimit(env, reservation) {
  if (!reservation) return;
  await env.DB.batch([reservation.sourceKey, reservation.globalKey].map(
    (rateKey) => env.DB.prepare(
      "UPDATE rate_limits SET request_count = MAX(request_count - 1, 0), updated_utc = ? " +
      "WHERE rate_key = ? AND rate_day = ?",
    ).bind(reservation.updatedUtc, rateKey, reservation.day),
  ));
}

function validateFeedbackUpload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("feedback upload must be an object");
  }
  if (payload.schema !== FEEDBACK_SCHEMA) throw new TypeError("feedback schema is unsupported");
  const caseId = cleanText(payload.case_id, 32, "case id", { required: true }).toLowerCase();
  const expectedSha = cleanText(payload.bundle_sha256, 64, "bundle SHA-256", { required: true }).toLowerCase();
  if (!/^[0-9a-f]{32}$/u.test(caseId) || !/^[0-9a-f]{64}$/u.test(expectedSha)) {
    throw new TypeError("feedback identity is invalid");
  }
  return {
    caseId,
    expectedSha,
    summary: cleanText(payload.summary, 240, "summary", { required: true }),
    replyEmail: normalizeEmail(payload.reply_email),
    appVersion: cleanText(payload.app_version, 80, "app version") || null,
    bundleBytes: decodeBase64(payload.bundle_base64),
  };
}

async function receiveFeedback(request, env) {
  requireBindings(env);
  const receivedUtc = new Date().toISOString();
  const rate = await rateLimit(request, env, receivedUtc);
  if (rate.response) return rate.response;
  if (!String(request.headers.get("content-type") || "").toLowerCase().startsWith("application/json")) {
    return errorResponse("unsupported_content_type", 415, "Feedback must use application/json.");
  }
  let upload;
  try {
    upload = validateFeedbackUpload(await readJsonBody(request, MAX_UPLOAD_JSON_BYTES));
  } catch (error) {
    return errorResponse("invalid_feedback", 400, String(error.message || error));
  }
  const observedSha = await sha256Hex(upload.bundleBytes);
  if (!timingSafeEqual(observedSha, upload.expectedSha)) {
    return errorResponse("bundle_sha256_mismatch", 400, "Feedback bundle SHA-256 does not match.");
  }
  const existing = await env.DB.prepare(
    "SELECT case_id, bundle_sha256, submission_id, received_utc FROM feedback_cases WHERE case_id = ?",
  ).bind(upload.caseId).first();
  if (existing) {
    if (!timingSafeEqual(String(existing.bundle_sha256), upload.expectedSha)) {
      return errorResponse("case_conflict", 409, "Case ID already exists with different content.");
    }
    return jsonResponse({
      ok: true,
      duplicate: true,
      case_id: upload.caseId,
      bundle_sha256: upload.expectedSha,
      received_utc: existing.received_utc,
    });
  }
  const submissionId = crypto.randomUUID();
  const objectKey = `feedback/${upload.caseId}/${upload.expectedSha}.zip`;
  let objectStored = false;
  try {
    await env.BUNDLES.put(objectKey, upload.bundleBytes, {
      httpMetadata: { contentType: "application/zip" },
      customMetadata: { case_id: upload.caseId, bundle_sha256: upload.expectedSha },
    });
    objectStored = true;
    await env.DB.prepare(
      "INSERT INTO feedback_cases(" +
        "case_id, bundle_sha256, submission_id, object_key, summary, reply_email, app_version, created_utc, received_utc, status" +
      ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
    ).bind(
      upload.caseId,
      upload.expectedSha,
      submissionId,
      objectKey,
      upload.summary,
      upload.replyEmail,
      upload.appVersion,
      receivedUtc,
      receivedUtc,
    ).run();
  } catch (error) {
    let raced = null;
    try {
      raced = await env.DB.prepare(
        "SELECT case_id, bundle_sha256, submission_id, received_utc FROM feedback_cases WHERE case_id = ?",
      ).bind(upload.caseId).first();
    } catch {
      raced = null;
    }
    if (raced && timingSafeEqual(String(raced.bundle_sha256), upload.expectedSha)) {
      const ownCommit = String(raced.submission_id || "") === submissionId;
      return jsonResponse({
        ok: true,
        duplicate: !ownCommit,
        case_id: upload.caseId,
        bundle_sha256: upload.expectedSha,
        received_utc: raced.received_utc,
      }, ownCommit ? 201 : 200);
    }
    const rollbacks = [releaseRateLimit(env, rate.reservation)];
    if (objectStored) rollbacks.push(env.BUNDLES.delete(objectKey));
    const rollbackResults = await Promise.allSettled(rollbacks);
    if (rollbackResults.some((result) => result.status === "rejected")) {
      throw new Error("feedback rollback failed");
    }
    throw error;
  }
  return jsonResponse({
    ok: true,
    duplicate: false,
    case_id: upload.caseId,
    bundle_sha256: upload.expectedSha,
    received_utc: receivedUtc,
  }, 201);
}

async function adminAuthorized(request, env) {
  const header = String(request.headers.get("authorization") || "");
  if (!header.startsWith("Bearer ")) return false;
  const observed = await sha256Hex(new TextEncoder().encode(header.slice(7)));
  const expected = await sha256Hex(new TextEncoder().encode(String(env.ADMIN_TOKEN || "")));
  return timingSafeEqual(observed, expected);
}

function replyMailto(replyEmail, caseId) {
  if (!replyEmail) return null;
  const subject = encodeURIComponent(`PeerBridge feedback ${caseId}`);
  return `mailto:${replyEmail}?subject=${subject}`;
}

async function listFeedback(request, env) {
  const url = new URL(request.url);
  const status = url.searchParams.get("status") || "new";
  const limit = Math.min(Math.max(Number(url.searchParams.get("limit") || "50"), 1), MAX_ADMIN_RESPONSE_CASES);
  if (!ALLOWED_CASE_STATUSES.has(status)) {
    return errorResponse("invalid_status", 400, "Feedback status is invalid.");
  }
  const rows = await env.DB.prepare(
    "SELECT case_id, bundle_sha256, summary, reply_email, app_version, created_utc, received_utc, status, digested_utc, replied_utc " +
    "FROM feedback_cases WHERE status = ? ORDER BY received_utc DESC LIMIT ?",
  ).bind(status, limit).all();
  return jsonResponse({
    schema: "peerbridge.feedback-inbox.v1",
    cases: (rows.results || []).map((row) => ({
      ...row,
      reply_mailto: replyMailto(row.reply_email, row.case_id),
    })),
  });
}

async function getFeedbackBundle(caseId, env) {
  if (!/^[0-9a-f]{32}$/u.test(caseId)) return errorResponse("invalid_case", 400, "Case ID is invalid.");
  const row = await env.DB.prepare(
    "SELECT bundle_sha256, object_key FROM feedback_cases WHERE case_id = ?",
  ).bind(caseId).first();
  if (!row) return errorResponse("not_found", 404, "Feedback case was not found.");
  const object = await env.BUNDLES.get(row.object_key);
  if (!object) return errorResponse("bundle_missing", 503, "Feedback bundle is unavailable.");
  return new Response(object.body, {
    headers: {
      "content-type": "application/zip",
      "content-disposition": `attachment; filename="peerbridge-feedback-${caseId}.zip"`,
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "x-peerbridge-bundle-sha256": row.bundle_sha256,
    },
  });
}

async function setFeedbackStatus(request, caseId, env) {
  if (!/^[0-9a-f]{32}$/u.test(caseId)) return errorResponse("invalid_case", 400, "Case ID is invalid.");
  let payload;
  try {
    payload = await readJsonBody(request, 4096);
  } catch (error) {
    return errorResponse("invalid_status", 400, String(error.message || error));
  }
  const status = String(payload?.status || "");
  if (!ALLOWED_CASE_STATUSES.has(status)) {
    return errorResponse("invalid_status", 400, "Feedback status is invalid.");
  }
  const replied = status === "replied" ? new Date().toISOString() : null;
  const result = await env.DB.prepare(
    "UPDATE feedback_cases SET status = ?, replied_utc = COALESCE(?, replied_utc) WHERE case_id = ?",
  ).bind(status, replied, caseId).run();
  if (!result.meta?.changes) return errorResponse("not_found", 404, "Feedback case was not found.");
  return jsonResponse({ ok: true, case_id: caseId, status });
}

function validateAnnouncement(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("announcement must be an object");
  }
  const announcementId = cleanText(payload.announcement_id, 64, "announcement id", { required: true });
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u.test(announcementId)) {
    throw new TypeError("announcement id is invalid");
  }
  const locale = String(payload.locale || "");
  const severity = String(payload.severity || "info");
  if (!ALLOWED_LOCALES.has(locale) || !ALLOWED_SEVERITIES.has(severity)) {
    throw new TypeError("announcement locale or severity is invalid");
  }
  const publishedUtc = new Date(payload.published_utc || new Date().toISOString());
  const expiresUtc = payload.expires_utc ? new Date(payload.expires_utc) : null;
  if (Number.isNaN(publishedUtc.valueOf()) || (expiresUtc && Number.isNaN(expiresUtc.valueOf()))) {
    throw new TypeError("announcement time is invalid");
  }
  return {
    announcementId,
    locale,
    title: cleanText(payload.title, 160, "announcement title", { required: true }),
    body: cleanText(payload.body, 4000, "announcement body", { required: true }),
    severity,
    linkUrl: normalizeHttpsUrl(payload.link_url),
    publishedUtc: publishedUtc.toISOString(),
    expiresUtc: expiresUtc ? expiresUtc.toISOString() : null,
  };
}

async function createAnnouncement(request, env) {
  let announcement;
  try {
    announcement = validateAnnouncement(await readJsonBody(request, 16 * 1024));
  } catch (error) {
    return errorResponse("invalid_announcement", 400, String(error.message || error));
  }
  const createdUtc = new Date().toISOString();
  try {
    await env.DB.prepare(
      "INSERT INTO announcements(announcement_id, locale, title, body, severity, link_url, published_utc, expires_utc, created_utc) " +
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ).bind(
      announcement.announcementId,
      announcement.locale,
      announcement.title,
      announcement.body,
      announcement.severity,
      announcement.linkUrl,
      announcement.publishedUtc,
      announcement.expiresUtc,
      createdUtc,
    ).run();
  } catch {
    return errorResponse("announcement_conflict", 409, "Announcement ID and locale already exist.");
  }
  return jsonResponse({
    ok: true,
    announcement_id: announcement.announcementId,
    locale: announcement.locale,
  }, 201);
}

async function listAnnouncements(request, env) {
  const url = new URL(request.url);
  const locale = url.searchParams.get("locale") || "en";
  const after = url.searchParams.get("after") || "1970-01-01T00:00:00.000Z";
  if (!ALLOWED_LOCALES.has(locale) || Number.isNaN(new Date(after).valueOf())) {
    return errorResponse("invalid_announcement_query", 400, "Announcement query is invalid.");
  }
  const now = new Date().toISOString();
  const rows = await env.DB.prepare(
    "SELECT announcement_id, locale, title, body, severity, link_url, published_utc, expires_utc " +
    "FROM announcements WHERE locale = ? AND published_utc > ? AND published_utc <= ? " +
    "AND (expires_utc IS NULL OR expires_utc > ?) ORDER BY published_utc ASC LIMIT 50",
  ).bind(locale, new Date(after).toISOString(), now, now).all();
  return jsonResponse({
    schema: ANNOUNCEMENT_SCHEMA,
    generated_utc: now,
    announcements: rows.results || [],
  }, 200, { "cache-control": "private, max-age=60" });
}

async function handleAdmin(request, env, pathname) {
  if (!(await adminAuthorized(request, env))) {
    return errorResponse("unauthorized", 401, "Admin authorization is required.");
  }
  if (request.method === "GET" && pathname === "/v1/admin/feedback") {
    return listFeedback(request, env);
  }
  let match = pathname.match(/^\/v1\/admin\/feedback\/([0-9a-f]{32})\/bundle$/u);
  if (request.method === "GET" && match) return getFeedbackBundle(match[1], env);
  match = pathname.match(/^\/v1\/admin\/feedback\/([0-9a-f]{32})\/status$/u);
  if (request.method === "POST" && match) return setFeedbackStatus(request, match[1], env);
  if (request.method === "POST" && pathname === "/v1/admin/announcements") {
    return createAnnouncement(request, env);
  }
  return errorResponse("not_found", 404, "Admin endpoint was not found.");
}

async function sendDigest(env) {
  if (!env.DIGEST_WEBHOOK_URL || !env.DIGEST_SHARED_SECRET) return;
  const endpoint = new URL(env.DIGEST_WEBHOOK_URL);
  if (endpoint.protocol !== "https:") throw new Error("digest webhook must use HTTPS");
  const rows = await env.DB.prepare(
    "SELECT case_id, summary, reply_email, app_version, received_utc FROM feedback_cases " +
    "WHERE digested_utc IS NULL ORDER BY received_utc ASC LIMIT 100",
  ).all();
  const cases = rows.results || [];
  if (!cases.length) return;
  const payload = JSON.stringify({
    schema: DIGEST_SCHEMA,
    generated_utc: new Date().toISOString(),
    cases,
  });
  const signature = await hmacHex(env.DIGEST_SHARED_SECRET, payload);
  const response = await fetch(endpoint.toString(), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-peerbridge-signature": `sha256=${signature}`,
    },
    body: payload,
    redirect: "error",
  });
  if (!response.ok) throw new Error(`digest webhook returned HTTP ${response.status}`);
  const digestedUtc = new Date().toISOString();
  await env.DB.batch(cases.map((item) => env.DB.prepare(
    "UPDATE feedback_cases SET digested_utc = ? WHERE case_id = ? AND digested_utc IS NULL",
  ).bind(digestedUtc, item.case_id)));
}

export async function handleRequest(request, env) {
  try {
    requireBindings(env);
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/v1/feedback") {
      return await receiveFeedback(request, env);
    }
    if (request.method === "GET" && url.pathname === "/v1/announcements") {
      return await listAnnouncements(request, env);
    }
    if (url.pathname.startsWith("/v1/admin/")) {
      return await handleAdmin(request, env, url.pathname);
    }
    if (request.method === "GET" && url.pathname === "/health") {
      return jsonResponse({ ok: true, service: "peerbridge-edge" });
    }
    return errorResponse("not_found", 404, "Endpoint was not found.");
  } catch (error) {
    const incidentId = crypto.randomUUID();
    const errorType = error instanceof TypeError ? "TypeError" : "Error";
    console.error(JSON.stringify({ incident_id: incidentId, error_type: errorType }));
    return jsonResponse({
      ok: false,
      error: "service_unavailable",
      message: "Service temporarily unavailable.",
      incident_id: incidentId,
    }, 503);
  }
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
  scheduled(_controller, env, ctx) {
    ctx.waitUntil(sendDigest(env));
  },
};

export const testing = {
  decodeBase64,
  normalizeEmail,
  normalizeHttpsUrl,
  rateLimit,
  releaseRateLimit,
  timingSafeEqual,
  validateAnnouncement,
  validateFeedbackUpload,
};
