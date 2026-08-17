const FEEDBACK_SCHEMA = "peerbridge.feedback-upload.v1";
const FEEDBACK_REPORT_SCHEMA = "peerbridge.feedback-report.v1";
const FEEDBACK_SECRET_SCHEMA = "peerbridge.feedback-secret-envelope.v1";
const FEEDBACK_SECRET_ALGORITHM = "RSA-OAEP-SHA256+A256GCM";
const ANNOUNCEMENT_SCHEMA = "peerbridge.announcement-feed.v1";
const DIGEST_SCHEMA = "peerbridge.feedback-digest.v1";

const MAX_UPLOAD_JSON_BYTES = 24 * 1024 * 1024;
const MAX_BUNDLE_BYTES = 16 * 1024 * 1024;
const MAX_EMAIL_BUNDLE_BYTES = 8 * 1024 * 1024;
const MAX_ADMIN_RESPONSE_CASES = 100;
const MAX_ATTEMPTS_PER_SOURCE_PER_DAY = 20;
const MAX_ATTEMPTS_GLOBAL_PER_DAY = 500;
const MAX_ACCEPTED_PER_SOURCE_PER_DAY = 5;
const MAX_ACCEPTED_GLOBAL_PER_DAY = 100;
const ALLOWED_LOCALES = new Set(["en", "zh-Hans", "zh-Hant"]);
const ALLOWED_SEVERITIES = new Set(["info", "important", "critical"]);
const ALLOWED_CASE_STATUSES = new Set(["new", "read", "replied", "closed"]);
const MIN_OPAQUE_SECRET_LENGTH = 32;
const MAX_OPAQUE_SECRET_LENGTH = 256;
const MAX_REPORT_BYTES = 256 * 1024;
const MAX_ENCRYPTED_CREDENTIAL_BYTES = 64 * 1024;
const MAX_ATTACHMENT_MEMBER_BYTES = 8 * 1024 * 1024;
const MAX_TOTAL_ATTACHMENT_BYTES = 16 * 1024 * 1024;
const MAX_EXPANDED_BUNDLE_BYTES = 64 * 1024 * 1024;
const MAX_COMPRESSION_RATIO = 250;
const FEEDBACK_RETENTION_DAYS = 30;
const MAX_RETENTION_PRUNE_CASES = 100;
const MAX_OBJECT_CLEANUP_RECORDS = 100;
const MAX_NOTIFICATION_RETRY_CASES = 25;
const MAX_NOTIFICATION_ATTEMPTS = 12;
const NOTIFICATION_CLAIM_TTL_MS = 5 * 60 * 1000;
const MAX_IMAGE_DIMENSION = 32768;
const MAX_IMAGE_PIXELS = 40000000;
const SAFE_FEEDBACK_SUMMARY = "Private PeerBridge feedback bundle";
const ATTACHMENT_MEMBER_PATTERN = /^attachments\/0[1-5]\.(?:gif|jpeg|jpg|json|log|png|txt|webp)$/u;
const FEEDBACK_UPLOAD_FIELDS = new Set([
  "schema",
  "case_id",
  "bundle_sha256",
  "bundle_base64",
]);
const FEEDBACK_SECRET_FIELDS = new Set([
  "schema",
  "case_id",
  "algorithm",
  "public_key_sha256",
  "associated_data_b64",
  "wrapped_key_b64",
  "nonce_b64",
  "ciphertext_b64",
]);

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
  if (!env?.DB) {
    throw new Error("required Worker bindings or secrets are unavailable");
  }
  for (const value of [env.RATE_SALT, env.ADMIN_TOKEN]) {
    const secret = typeof value === "string" ? value : "";
    if (
      secret.length < MIN_OPAQUE_SECRET_LENGTH
      || secret.length > MAX_OPAQUE_SECRET_LENGTH
      || /[\u0000-\u0020\u007f]/u.test(secret)
    ) {
      throw new Error("required Worker bindings or secrets are unavailable");
    }
  }
}

function cleanText(value, maximum, label, { required = false } = {}) {
  const text = typeof value === "string" ? value.trim() : "";
  if (required && !text) {
    throw new TypeError(`${label} is required`);
  }
  if (text.length > maximum || /[\u0000-\u001f\u007f]/u.test(text)) {
    throw new TypeError(`${label} is invalid`);
  }
  return text;
}

function normalizeEmail(value) {
  const text = cleanText(value, 320, "reply email");
  if (!text) return null;
  if (!/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/u.test(text)) {
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

function normalizeAppsScriptEndpoint(value) {
  const text = String(value || "").trim();
  let parsed;
  try {
    parsed = new URL(text);
  } catch {
    throw new TypeError("Apps Script notification endpoint is invalid");
  }
  if (
    parsed.protocol !== "https:"
    || parsed.hostname !== "script.google.com"
    || parsed.port
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || !/^\/macros\/s\/[A-Za-z0-9_-]+\/exec$/u.test(parsed.pathname)
  ) {
    throw new TypeError("Apps Script notification endpoint must be a deployed /exec URL");
  }
  return parsed.toString();
}

function normalizeAppsScriptReceiptRedirect(value) {
  let parsed;
  try {
    parsed = new URL(String(value || ""));
  } catch {
    throw new Error("Apps Script notification redirect is invalid");
  }
  if (
    parsed.protocol !== "https:"
    || parsed.hostname !== "script.googleusercontent.com"
    || parsed.port
    || parsed.username
    || parsed.password
    || parsed.hash
  ) {
    throw new Error("Apps Script notification redirect is not trusted");
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

function encodeBase64(bytes) {
  const payload = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (let offset = 0; offset < payload.byteLength; offset += 0x8000) {
    binary += String.fromCharCode(...payload.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function decodeCanonicalBase64Field(value, label, minimumBytes, maximumBytes) {
  if (
    typeof value !== "string" || !value || value.length % 4 !== 0
    || value.length > Math.ceil(maximumBytes / 3) * 4 + 4
    || !/^[A-Za-z0-9+/]*={0,2}$/u.test(value)
  ) {
    throw new TypeError(`${label} is not canonical base64`);
  }
  let binary;
  try {
    binary = atob(value);
  } catch {
    throw new TypeError(`${label} is not valid base64`);
  }
  if (
    btoa(binary) !== value
    || binary.length < minimumBytes
    || binary.length > maximumBytes
  ) {
    throw new TypeError(`${label} size or encoding is invalid`);
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function bytesEqual(left, right) {
  if (!(left instanceof Uint8Array) || !(right instanceof Uint8Array) || left.length !== right.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

function startsWithBytes(payload, prefix) {
  if (!(payload instanceof Uint8Array) || payload.length < prefix.length) return false;
  for (let index = 0; index < prefix.length; index += 1) {
    if (payload[index] !== prefix[index]) return false;
  }
  return true;
}

function decodeTextAttachment(name, payload) {
  let encoding = "utf-8";
  let offset = 0;
  if (startsWithBytes(payload, [0xff, 0xfe, 0x00, 0x00])
      || startsWithBytes(payload, [0x00, 0x00, 0xfe, 0xff])) {
    return null;
  }
  if (startsWithBytes(payload, [0xff, 0xfe])) {
    encoding = "utf-16le";
    offset = 2;
  } else if (startsWithBytes(payload, [0xfe, 0xff])) {
    encoding = "utf-16be";
    offset = 2;
  } else if (startsWithBytes(payload, [0xef, 0xbb, 0xbf])) {
    offset = 3;
  }
  let text;
  try {
    text = new TextDecoder(encoding, { fatal: true }).decode(payload.subarray(offset));
  } catch {
    return null;
  }
  if (text.includes("\u0000")) return null;
  if (name.endsWith(".json")) {
    try {
      JSON.parse(text);
    } catch {
      return null;
    }
  }
  return text;
}

function readUint16BE(payload, offset) {
  return (payload[offset] << 8) | payload[offset + 1];
}

function readUint16LE(payload, offset) {
  return payload[offset] | (payload[offset + 1] << 8);
}

function readUint24LE(payload, offset) {
  return payload[offset] | (payload[offset + 1] << 8) | (payload[offset + 2] << 16);
}

function readUint32BE(payload, offset) {
  return (
    (payload[offset] * 0x1000000)
    + (payload[offset + 1] << 16)
    + (payload[offset + 2] << 8)
    + payload[offset + 3]
  ) >>> 0;
}

function readUint32LE(payload, offset) {
  return (
    payload[offset]
    + (payload[offset + 1] << 8)
    + (payload[offset + 2] << 16)
    + (payload[offset + 3] * 0x1000000)
  ) >>> 0;
}

function safeImageDimensions(width, height) {
  return Number.isSafeInteger(width) && Number.isSafeInteger(height)
    && width > 0 && height > 0
    && width <= MAX_IMAGE_DIMENSION && height <= MAX_IMAGE_DIMENSION
    && width * height <= MAX_IMAGE_PIXELS;
}

function validatePngPayload(payload) {
  if (!startsWithBytes(payload, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) {
    return false;
  }
  let offset = 8;
  let sawHeader = false;
  let sawData = false;
  let dataEnded = false;
  while (offset < payload.length) {
    if (payload.length - offset < 12) return false;
    const length = readUint32BE(payload, offset);
    const chunkEnd = offset + 12 + length;
    if (!Number.isSafeInteger(chunkEnd) || chunkEnd > payload.length) return false;
    const typeStart = offset + 4;
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    const type = String.fromCharCode(...payload.subarray(typeStart, typeStart + 4));
    if (readUint32BE(payload, dataEnd) !== crc32(payload.subarray(typeStart, dataEnd))) {
      return false;
    }
    if (!sawHeader) {
      if (type !== "IHDR" || length !== 13) return false;
      const width = readUint32BE(payload, dataStart);
      const height = readUint32BE(payload, dataStart + 4);
      const depth = payload[dataStart + 8];
      const color = payload[dataStart + 9];
      const allowedDepths = new Map([
        [0, new Set([1, 2, 4, 8, 16])],
        [2, new Set([8, 16])],
        [3, new Set([1, 2, 4, 8])],
        [4, new Set([8, 16])],
        [6, new Set([8, 16])],
      ]);
      if (
        !safeImageDimensions(width, height)
        || !allowedDepths.get(color)?.has(depth)
        || payload[dataStart + 10] !== 0
        || payload[dataStart + 11] !== 0
        || ![0, 1].includes(payload[dataStart + 12])
      ) return false;
      sawHeader = true;
    } else if (type === "IHDR") {
      return false;
    }
    if (type === "IDAT") {
      if (dataEnded) return false;
      sawData = true;
    } else if (sawData && type !== "IEND") {
      dataEnded = true;
    }
    if (type === "IEND") {
      return length === 0 && sawHeader && sawData && chunkEnd === payload.length;
    }
    offset = chunkEnd;
  }
  return false;
}

const JPEG_SOF_MARKERS = new Set([
  0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf,
]);

function validateJpegPayload(payload) {
  if (payload.length < 4 || payload[0] !== 0xff || payload[1] !== 0xd8) return false;
  let offset = 2;
  let sawFrame = false;
  let sawScan = false;
  while (offset < payload.length) {
    if (payload[offset] !== 0xff) return false;
    while (offset < payload.length && payload[offset] === 0xff) offset += 1;
    if (offset >= payload.length) return false;
    const marker = payload[offset];
    offset += 1;
    if (marker === 0xd9) return sawFrame && sawScan && offset === payload.length;
    if (marker === 0x00 || marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) {
      return false;
    }
    if (offset + 2 > payload.length) return false;
    const segmentLength = readUint16BE(payload, offset);
    const segmentEnd = offset + segmentLength;
    if (segmentLength < 2 || segmentEnd > payload.length) return false;
    if (JPEG_SOF_MARKERS.has(marker)) {
      if (segmentLength < 8) return false;
      const height = readUint16BE(payload, offset + 3);
      const width = readUint16BE(payload, offset + 5);
      if (!safeImageDimensions(width, height)) return false;
      sawFrame = true;
    }
    if (marker !== 0xda) {
      offset = segmentEnd;
      continue;
    }
    sawScan = true;
    offset = segmentEnd;
    while (offset < payload.length) {
      const markerOffset = payload.indexOf(0xff, offset);
      if (markerOffset < 0 || markerOffset + 1 >= payload.length) return false;
      const nextByte = payload[markerOffset + 1];
      if (nextByte === 0x00 || (nextByte >= 0xd0 && nextByte <= 0xd7)) {
        offset = markerOffset + 2;
        continue;
      }
      if (nextByte === 0xff) {
        offset = markerOffset + 1;
        continue;
      }
      offset = markerOffset;
      break;
    }
  }
  return false;
}

function skipGifSubBlocks(payload, start) {
  let offset = start;
  while (offset < payload.length) {
    const length = payload[offset];
    offset += 1;
    if (length === 0) return offset;
    offset += length;
    if (offset > payload.length) return -1;
  }
  return -1;
}

function validateGifPayload(payload) {
  const validHeader = startsWithBytes(payload, [0x47, 0x49, 0x46, 0x38, 0x37, 0x61])
    || startsWithBytes(payload, [0x47, 0x49, 0x46, 0x38, 0x39, 0x61]);
  if (!validHeader || payload.length < 14) return false;
  if (!safeImageDimensions(readUint16LE(payload, 6), readUint16LE(payload, 8))) return false;
  const packed = payload[10];
  let offset = 13 + ((packed & 0x80) ? 3 * (2 ** ((packed & 0x07) + 1)) : 0);
  let sawImage = false;
  while (offset < payload.length) {
    const introducer = payload[offset];
    offset += 1;
    if (introducer === 0x3b) return sawImage && offset === payload.length;
    if (introducer === 0x21) {
      if (offset >= payload.length) return false;
      offset = skipGifSubBlocks(payload, offset + 1);
      if (offset < 0) return false;
      continue;
    }
    if (introducer !== 0x2c || offset + 9 > payload.length) return false;
    if (!safeImageDimensions(readUint16LE(payload, offset + 4), readUint16LE(payload, offset + 6))) {
      return false;
    }
    const imagePacked = payload[offset + 8];
    offset += 9;
    if (imagePacked & 0x80) offset += 3 * (2 ** ((imagePacked & 0x07) + 1));
    if (offset >= payload.length) return false;
    offset = skipGifSubBlocks(payload, offset + 1);
    if (offset < 0) return false;
    sawImage = true;
  }
  return false;
}

function validateWebpPayload(payload) {
  if (
    payload.length < 20
    || !startsWithBytes(payload, [0x52, 0x49, 0x46, 0x46])
    || String.fromCharCode(...payload.subarray(8, 12)) !== "WEBP"
    || readUint32LE(payload, 4) + 8 !== payload.length
  ) return false;
  let offset = 12;
  let dimensions = null;
  let sawImageData = false;
  while (offset < payload.length) {
    if (offset + 8 > payload.length) return false;
    const type = String.fromCharCode(...payload.subarray(offset, offset + 4));
    const length = readUint32LE(payload, offset + 4);
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    const paddedEnd = dataEnd + (length & 1);
    if (!Number.isSafeInteger(paddedEnd) || paddedEnd > payload.length) return false;
    if (type === "VP8X") {
      if (length !== 10) return false;
      dimensions = [readUint24LE(payload, dataStart + 4) + 1, readUint24LE(payload, dataStart + 7) + 1];
    } else if (type === "VP8 ") {
      if (
        length < 10 || payload[dataStart + 3] !== 0x9d
        || payload[dataStart + 4] !== 0x01 || payload[dataStart + 5] !== 0x2a
      ) return false;
      dimensions = [readUint16LE(payload, dataStart + 6) & 0x3fff, readUint16LE(payload, dataStart + 8) & 0x3fff];
      sawImageData = true;
    } else if (type === "VP8L") {
      if (length < 5 || payload[dataStart] !== 0x2f) return false;
      const bits = readUint32LE(payload, dataStart + 1);
      dimensions = [(bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1];
      sawImageData = true;
    } else if (type === "ANMF") {
      if (length < 16) return false;
      const frameDimensions = [
        readUint24LE(payload, dataStart + 6) + 1,
        readUint24LE(payload, dataStart + 9) + 1,
      ];
      if (!safeImageDimensions(...frameDimensions)) return false;
      sawImageData = true;
    }
    offset = paddedEnd;
  }
  return offset === payload.length && dimensions !== null
    && safeImageDimensions(...dimensions) && sawImageData;
}

function attachmentPayloadIsValid(name, payload) {
  if (name.endsWith(".png")) {
    return validatePngPayload(payload);
  }
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) {
    return validateJpegPayload(payload);
  }
  if (name.endsWith(".gif")) {
    return validateGifPayload(payload);
  }
  if (name.endsWith(".webp")) {
    return validateWebpPayload(payload);
  }
  return /\.(?:json|log|txt)$/u.test(name) && decodeTextAttachment(name, payload) !== null;
}

function validateEncryptedCredentialEnvelope(bytes, caseId) {
  let envelope;
  try {
    envelope = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new TypeError("feedback encrypted credential is not valid UTF-8 JSON");
  }
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
    throw new TypeError("feedback encrypted credential envelope is invalid");
  }
  const fields = Object.keys(envelope);
  if (
    fields.length !== FEEDBACK_SECRET_FIELDS.size
    || fields.some((field) => !FEEDBACK_SECRET_FIELDS.has(field))
    || envelope.schema !== FEEDBACK_SECRET_SCHEMA
    || envelope.algorithm !== FEEDBACK_SECRET_ALGORITHM
    || envelope.case_id !== caseId
    || !/^[0-9a-f]{64}$/u.test(String(envelope.public_key_sha256 || ""))
  ) {
    throw new TypeError("feedback encrypted credential envelope is invalid");
  }
  const expectedAssociatedData = new TextEncoder().encode(`${FEEDBACK_SECRET_SCHEMA}:${caseId}`);
  const associatedData = decodeCanonicalBase64Field(
    envelope.associated_data_b64,
    "feedback encrypted credential associated data",
    expectedAssociatedData.length,
    expectedAssociatedData.length,
  );
  if (!bytesEqual(associatedData, expectedAssociatedData)) {
    throw new TypeError("feedback encrypted credential is bound to the wrong case");
  }
  decodeCanonicalBase64Field(
    envelope.wrapped_key_b64,
    "feedback encrypted credential wrapped key",
    384,
    1024,
  );
  decodeCanonicalBase64Field(
    envelope.nonce_b64,
    "feedback encrypted credential nonce",
    12,
    12,
  );
  decodeCanonicalBase64Field(
    envelope.ciphertext_b64,
    "feedback encrypted credential ciphertext",
    17,
    MAX_ENCRYPTED_CREDENTIAL_BYTES,
  );
}

function zipByte(bytes, offset) {
  if (offset < 0 || offset >= bytes.length) throw new TypeError("feedback ZIP is truncated");
  return Number(bytes[offset]) & 0xff;
}

function zipUint16(bytes, offset) {
  return zipByte(bytes, offset) + zipByte(bytes, offset + 1) * 0x100;
}

function zipUint32(bytes, offset) {
  return zipByte(bytes, offset)
    + zipByte(bytes, offset + 1) * 0x100
    + zipByte(bytes, offset + 2) * 0x10000
    + zipByte(bytes, offset + 3) * 0x1000000;
}

function safeZipName(bytes, offset, length) {
  if (length < 1 || length > 512 || offset + length > bytes.length) {
    throw new TypeError("feedback ZIP entry name is invalid");
  }
  let name = "";
  for (let index = 0; index < length; index += 1) {
    const value = zipByte(bytes, offset + index);
    if (value === 0 || value === 58 || value === 92) {
      throw new TypeError("feedback ZIP entry path is unsafe");
    }
    name += value < 128 ? String.fromCharCode(value) : "?";
  }
  if (name.startsWith("/") || name.split("/").some((part) => part === "..")) {
    throw new TypeError("feedback ZIP entry path is unsafe");
  }
  return name;
}

function allowedZipMember(name) {
  return name === "report.json"
    || name === "encrypted-credential.json"
    || ATTACHMENT_MEMBER_PATTERN.test(name);
}

function zipMemberLimit(name) {
  if (name === "report.json") return MAX_REPORT_BYTES;
  if (name === "encrypted-credential.json") return MAX_ENCRYPTED_CREDENTIAL_BYTES;
  if (ATTACHMENT_MEMBER_PATTERN.test(name)) return MAX_ATTACHMENT_MEMBER_BYTES;
  return 0;
}

const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < table.length; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
    }
    table[index] = value >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value = (value >>> 8) ^ CRC32_TABLE[(value ^ (Number(byte) & 0xff)) & 0xff];
  }
  return (value ^ 0xffffffff) >>> 0;
}

function validateZipBundle(bytes) {
  if (!bytes || bytes.length < 22) throw new TypeError("feedback bundle is not a ZIP archive");
  const minimumEocd = Math.max(0, bytes.length - 65557);
  let eocd = -1;
  for (let offset = bytes.length - 22; offset >= minimumEocd; offset -= 1) {
    if (zipUint32(bytes, offset) === 0x06054b50) {
      eocd = offset;
      break;
    }
  }
  if (eocd < 0) throw new TypeError("feedback ZIP end record is missing");
  const disk = zipUint16(bytes, eocd + 4);
  const centralDisk = zipUint16(bytes, eocd + 6);
  const diskEntries = zipUint16(bytes, eocd + 8);
  const totalEntries = zipUint16(bytes, eocd + 10);
  const centralSize = zipUint32(bytes, eocd + 12);
  const centralOffset = zipUint32(bytes, eocd + 16);
  const commentLength = zipUint16(bytes, eocd + 20);
  if (
    disk !== 0 || centralDisk !== 0 || diskEntries !== totalEntries
    || totalEntries < 1 || totalEntries > 64
    || centralOffset + centralSize !== eocd
    || eocd + 22 + commentLength !== bytes.length
  ) {
    throw new TypeError("feedback ZIP structure is invalid");
  }
  let cursor = centralOffset;
  let totalUncompressed = 0;
  let totalAttachmentBytes = 0;
  let hasReport = false;
  const localOffsets = new Set();
  const names = new Set();
  const entries = [];
  for (let entryIndex = 0; entryIndex < totalEntries; entryIndex += 1) {
    if (cursor + 46 > eocd || zipUint32(bytes, cursor) !== 0x02014b50) {
      throw new TypeError("feedback ZIP central directory is invalid");
    }
    const flags = zipUint16(bytes, cursor + 8);
    const method = zipUint16(bytes, cursor + 10);
    const entryCrc32 = zipUint32(bytes, cursor + 16) >>> 0;
    const compressedSize = zipUint32(bytes, cursor + 20);
    const uncompressedSize = zipUint32(bytes, cursor + 24);
    const nameLength = zipUint16(bytes, cursor + 28);
    const extraLength = zipUint16(bytes, cursor + 30);
    const entryCommentLength = zipUint16(bytes, cursor + 32);
    const externalAttributes = zipUint32(bytes, cursor + 38) >>> 0;
    const localOffset = zipUint32(bytes, cursor + 42);
    const next = cursor + 46 + nameLength + extraLength + entryCommentLength;
    if (
      (flags & ~0x0800) !== 0 || ![0, 8].includes(method)
      || compressedSize === 0xffffffff || uncompressedSize === 0xffffffff
      || next > eocd
      || localOffsets.has(localOffset) || extraLength !== 0 || entryCommentLength !== 0
      || (((externalAttributes >>> 16) & 0xf000) === 0xa000)
    ) {
      throw new TypeError("feedback ZIP entry is unsupported");
    }
    const name = safeZipName(bytes, cursor + 46, nameLength);
    if (!allowedZipMember(name) || names.has(name)) {
      throw new TypeError("feedback ZIP contains an unsupported or duplicate member");
    }
    const memberLimit = zipMemberLimit(name);
    if (!memberLimit || uncompressedSize > memberLimit) {
      throw new TypeError("feedback ZIP member size is invalid");
    }
    if (
      method === 8
      && uncompressedSize > 0
      && (compressedSize === 0 || uncompressedSize > compressedSize * MAX_COMPRESSION_RATIO)
    ) {
      throw new TypeError("feedback ZIP compression ratio is unsafe");
    }
    if (ATTACHMENT_MEMBER_PATTERN.test(name)) {
      totalAttachmentBytes += uncompressedSize;
      if (totalAttachmentBytes > MAX_TOTAL_ATTACHMENT_BYTES) {
        throw new TypeError("feedback ZIP attachments are too large");
      }
    }
    hasReport ||= name === "report.json";
    totalUncompressed += uncompressedSize;
    if (totalUncompressed > MAX_EXPANDED_BUNDLE_BYTES) {
      throw new TypeError("feedback ZIP expansion is too large");
    }
    if (localOffset + 30 > centralOffset || zipUint32(bytes, localOffset) !== 0x04034b50) {
      throw new TypeError("feedback ZIP local header is invalid");
    }
    const localFlags = zipUint16(bytes, localOffset + 6);
    const localMethod = zipUint16(bytes, localOffset + 8);
    const localCrc32 = zipUint32(bytes, localOffset + 14) >>> 0;
    const localCompressedSize = zipUint32(bytes, localOffset + 18);
    const localUncompressedSize = zipUint32(bytes, localOffset + 22);
    const localNameLength = zipUint16(bytes, localOffset + 26);
    const localExtraLength = zipUint16(bytes, localOffset + 28);
    const dataOffset = localOffset + 30 + localNameLength + localExtraLength;
    if (
      localFlags !== flags || localMethod !== method || localNameLength !== nameLength
      || localExtraLength !== 0 || localCrc32 !== entryCrc32
      || localCompressedSize !== compressedSize || localUncompressedSize !== uncompressedSize
      || dataOffset + compressedSize > centralOffset
    ) {
      throw new TypeError("feedback ZIP local entry is inconsistent");
    }
    for (let nameIndex = 0; nameIndex < nameLength; nameIndex += 1) {
      if (zipByte(bytes, localOffset + 30 + nameIndex) !== zipByte(bytes, cursor + 46 + nameIndex)) {
        throw new TypeError("feedback ZIP entry names do not match");
      }
    }
    localOffsets.add(localOffset);
    names.add(name);
    entries.push({
      name,
      method,
      crc32: entryCrc32,
      compressedSize,
      uncompressedSize,
      localOffset,
      dataOffset,
      dataEnd: dataOffset + compressedSize,
    });
    cursor = next;
  }
  if (cursor !== eocd || !hasReport) {
    throw new TypeError("feedback ZIP does not contain the sealed report");
  }
  const ordered = [...entries].sort((left, right) => left.localOffset - right.localOffset);
  let expectedOffset = 0;
  for (const entry of ordered) {
    if (entry.localOffset !== expectedOffset) {
      throw new TypeError("feedback ZIP contains an unindexed or overlapping local entry");
    }
    expectedOffset = entry.dataEnd;
  }
  if (expectedOffset !== centralOffset) {
    throw new TypeError("feedback ZIP local data does not end at the central directory");
  }
  const attachmentNames = [...names]
    .filter((name) => ATTACHMENT_MEMBER_PATTERN.test(name))
    .sort();
  attachmentNames.forEach((name, index) => {
    const expectedPrefix = `attachments/${String(index + 1).padStart(2, "0")}.`;
    if (!name.startsWith(expectedPrefix)) {
      throw new TypeError("feedback ZIP attachment indices are not canonical");
    }
  });
  return entries;
}

async function readBoundedStream(stream, maximumBytes) {
  const reader = stream.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array) || length + value.byteLength > maximumBytes) {
        await reader.cancel("feedback ZIP expansion exceeded its declared limit");
        throw new TypeError("feedback ZIP expansion is too large");
      }
      chunks.push(value);
      length += value.byteLength;
    }
  } catch (error) {
    if (error instanceof TypeError && error.message === "feedback ZIP expansion is too large") {
      throw error;
    }
    throw new TypeError("feedback ZIP member could not be decompressed");
  } finally {
    reader.releaseLock();
  }
  const payload = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    payload.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return payload;
}

async function decompressedZipEntry(bytes, entry, remainingBytes) {
  const maximumBytes = Math.min(entry.uncompressedSize, remainingBytes);
  const compressed = bytes.subarray(entry.dataOffset, entry.dataEnd);
  let payload;
  if (entry.method === 0) {
    if (compressed.byteLength > maximumBytes) {
      throw new TypeError("feedback ZIP expansion is too large");
    }
    payload = compressed;
  } else {
    let stream;
    try {
      stream = new Blob([compressed]).stream().pipeThrough(
        new DecompressionStream("deflate-raw"),
      );
    } catch {
      throw new TypeError("feedback ZIP member could not be decompressed");
    }
    payload = await readBoundedStream(stream, maximumBytes);
  }
  if (payload.length !== entry.uncompressedSize || crc32(payload) !== entry.crc32) {
    throw new TypeError("feedback ZIP member size or CRC is invalid");
  }
  return payload;
}

async function validateZipBundleContents(bytes, entries, caseId) {
  const members = new Map();
  let expanded = 0;
  for (const entry of entries) {
    const payload = await decompressedZipEntry(
      bytes, entry, MAX_EXPANDED_BUNDLE_BYTES - expanded,
    );
    expanded += payload.length;
    if (expanded > MAX_EXPANDED_BUNDLE_BYTES) {
      throw new TypeError("feedback ZIP expansion is too large");
    }
    members.set(entry.name, payload);
  }
  const reportBytes = members.get("report.json");
  if (!reportBytes || reportBytes.length > MAX_REPORT_BYTES) {
    throw new TypeError("feedback report size is invalid");
  }
  let report;
  try {
    report = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(reportBytes));
  } catch {
    throw new TypeError("feedback report is not valid UTF-8 JSON");
  }
  if (
    !report || typeof report !== "object" || Array.isArray(report)
    || report.schema !== FEEDBACK_REPORT_SCHEMA || report.case_id !== caseId
    || typeof report.encrypted_credential_included !== "boolean"
    || !Array.isArray(report.attachments) || report.attachments.length > 5
  ) {
    throw new TypeError("feedback report schema or identity is invalid");
  }
  if (report.encrypted_credential_included !== members.has("encrypted-credential.json")) {
    throw new TypeError("feedback encrypted-credential manifest is inconsistent");
  }
  const encryptedCredential = members.get("encrypted-credential.json");
  if (encryptedCredential) {
    validateEncryptedCredentialEnvelope(encryptedCredential, caseId);
  }
  const manifestNames = new Set();
  for (const item of report.attachments) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new TypeError("feedback attachment manifest is invalid");
    }
    const name = String(item.archive_name || "");
    const payload = members.get(name);
    if (
      !ATTACHMENT_MEMBER_PATTERN.test(name) || manifestNames.has(name) || !payload
      || item.bytes !== payload.length
      || typeof item.sha256 !== "string"
      || !timingSafeEqual(item.sha256.toLowerCase(), await sha256Hex(payload))
      || !attachmentPayloadIsValid(name, payload)
    ) {
      throw new TypeError("feedback attachment manifest does not match the ZIP member");
    }
    manifestNames.add(name);
  }
  const actualNames = [...members.keys()].filter((name) => ATTACHMENT_MEMBER_PATTERN.test(name));
  if (actualNames.length !== manifestNames.size) {
    throw new TypeError("feedback ZIP contains an unmanifested attachment");
  }
  return report;
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
  const declaredText = request.headers.get("content-length");
  if (declaredText !== null) {
    const declared = Number(declaredText);
    if (!Number.isSafeInteger(declared) || declared < 0 || declared > maximumBytes) {
      throw new TypeError("request body is too large");
    }
  }
  if (!request.body) throw new TypeError("request body size is invalid");
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maximumBytes) {
      await reader.cancel("request body is too large");
      throw new TypeError("request body is too large");
    }
    chunks.push(value);
  }
  if (!total) {
    throw new TypeError("request body size is invalid");
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new TypeError("request body is not valid UTF-8 JSON");
  }
}

async function rateLimit(request, env, now, phase = "accepted") {
  const day = now.slice(0, 10);
  const source = request.headers.get("cf-connecting-ip") || "unknown";
  const sourceHash = await hmacHex(env.RATE_SALT, `${day}\n${source}`);
  const rateKeys = phase === "attempt"
    ? [`attempt-source:${sourceHash}`, "attempt-global"]
    : [`accepted-source:${sourceHash}`, "accepted-global"];
  const statements = rateKeys.map((rateKey) => env.DB.prepare(
    "INSERT INTO rate_limits(rate_key, rate_day, request_count, updated_utc) VALUES (?, ?, 1, ?) " +
    "ON CONFLICT(rate_key, rate_day) DO UPDATE SET request_count = request_count + 1, updated_utc = excluded.updated_utc",
  ).bind(rateKey, day, now));
  try {
    await env.DB.batch(statements);
  } catch (error) {
    const rows = await env.DB.prepare(
      `SELECT rate_key, request_count FROM rate_limits WHERE rate_day = ? AND rate_key IN (${rateKeys.map(() => "?").join(", ")})`,
    ).bind(day, ...rateKeys).all();
    const counts = new Map((rows.results || []).map(
      (row) => [row.rate_key, Number(row.request_count)],
    ));
    const sourceKey = rateKeys[0];
    const sourceLimit = phase === "attempt"
      ? MAX_ATTEMPTS_PER_SOURCE_PER_DAY
      : MAX_ACCEPTED_PER_SOURCE_PER_DAY;
    if ((counts.get(sourceKey) || 0) >= sourceLimit) {
      return {
        response: errorResponse(
          phase === "attempt" ? "source_attempt_rate_limited" : "source_rate_limited",
          429,
          "Daily feedback limit reached for this source.",
        ),
        reservation: null,
      };
    }
    if (phase === "attempt" && (counts.get("attempt-global") || 0) >= MAX_ATTEMPTS_GLOBAL_PER_DAY) {
      return {
        response: errorResponse(
          "service_attempt_rate_limited",
          429,
          "Daily feedback attempt limit reached.",
        ),
        reservation: null,
      };
    }
    if (phase === "accepted" && (counts.get("accepted-global") || 0) >= MAX_ACCEPTED_GLOBAL_PER_DAY) {
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
    reservation: { day, rateKeys, updatedUtc: now },
  };
}

async function releaseRateLimit(env, reservation) {
  if (!reservation) return;
  await env.DB.batch(reservation.rateKeys.map(
    (rateKey) => env.DB.prepare(
      "UPDATE rate_limits SET request_count = MAX(request_count - 1, 0), updated_utc = ? " +
      "WHERE rate_key = ? AND rate_day = ?",
    ).bind(reservation.updatedUtc, rateKey, reservation.day),
  ));
}

async function recordObjectCleanup(env, upload, objectKey, reason, now) {
  await env.DB.prepare(
    "INSERT INTO feedback_object_cleanup(" +
      "object_key, case_id, bundle_sha256, reason, created_utc, updated_utc, attempt_count" +
    ") VALUES (?, ?, ?, ?, ?, ?, 0) " +
    "ON CONFLICT(object_key) DO UPDATE SET " +
      "reason = excluded.reason, updated_utc = excluded.updated_utc",
  ).bind(
    objectKey,
    upload.caseId,
    upload.expectedSha,
    reason,
    now,
    now,
  ).run();
}

async function validateFeedbackUpload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("feedback upload must be an object");
  }
  const fields = Object.keys(payload);
  if (
    fields.length !== FEEDBACK_UPLOAD_FIELDS.size
    || fields.some((field) => !FEEDBACK_UPLOAD_FIELDS.has(field))
  ) {
    throw new TypeError("feedback upload fields are invalid");
  }
  if (payload.schema !== FEEDBACK_SCHEMA) throw new TypeError("feedback schema is unsupported");
  const caseId = cleanText(payload.case_id, 32, "case id", { required: true }).toLowerCase();
  const expectedSha = cleanText(payload.bundle_sha256, 64, "bundle SHA-256", { required: true }).toLowerCase();
  if (!/^[0-9a-f]{32}$/u.test(caseId) || !/^[0-9a-f]{64}$/u.test(expectedSha)) {
    throw new TypeError("feedback identity is invalid");
  }
  const bundleBytes = decodeBase64(payload.bundle_base64);
  const entries = validateZipBundle(bundleBytes);
  const report = await validateZipBundleContents(bundleBytes, entries, caseId);
  const runtime = report.runtime;
  if (!runtime || typeof runtime !== "object" || Array.isArray(runtime)) {
    throw new TypeError("feedback report runtime metadata is invalid");
  }
  let replyEmail = null;
  try {
    replyEmail = normalizeEmail(report.contact);
  } catch {
    replyEmail = null;
  }
  return {
    caseId,
    expectedSha,
    summary: cleanText(report.summary, 240, "summary", { required: true }),
    replyEmail,
    appVersion: cleanText(runtime.app_version, 80, "app version", { required: true }),
    bundleBase64: payload.bundle_base64,
    bundleBytes,
  };
}

async function receiveFeedback(request, env) {
  requireBindings(env);
  const receivedUtc = new Date().toISOString();
  const attemptRate = await rateLimit(request, env, receivedUtc, "attempt");
  if (attemptRate.response) return attemptRate.response;
  const reject = (code, status, message) => errorResponse(code, status, message);
  if (!String(request.headers.get("content-type") || "").toLowerCase().startsWith("application/json")) {
    return reject("unsupported_content_type", 415, "Feedback must use application/json.");
  }
  let upload;
  try {
    upload = await validateFeedbackUpload(await readJsonBody(request, MAX_UPLOAD_JSON_BYTES));
  } catch (error) {
    return reject("invalid_feedback", 400, String(error.message || error));
  }
  const observedSha = await sha256Hex(upload.bundleBytes);
  if (!timingSafeEqual(observedSha, upload.expectedSha)) {
    return reject("bundle_sha256_mismatch", 400, "Feedback bundle SHA-256 does not match.");
  }
  let existing;
  try {
    existing = await env.DB.prepare(
      "SELECT case_id, bundle_sha256, submission_id, received_utc, notification_status " +
      "FROM feedback_cases WHERE case_id = ?",
    ).bind(upload.caseId).first();
  } catch (error) {
    throw error;
  }
  if (existing) {
    if (!timingSafeEqual(String(existing.bundle_sha256), upload.expectedSha)) {
      return reject("case_conflict", 409, "Case ID already exists with different content.");
    }
    let notificationSent = existing.notification_status === "sent";
    if (["pending", "failed"].includes(existing.notification_status)) {
      notificationSent = await attemptFeedbackNotification(
        env, upload, existing.received_utc,
      );
    }
    return jsonResponse({
      ok: true,
      duplicate: true,
      case_id: upload.caseId,
      bundle_sha256: upload.expectedSha,
      received_utc: existing.received_utc,
      notification_sent: notificationSent,
    });
  }
  const acceptedRate = await rateLimit(request, env, receivedUtc, "accepted");
  if (acceptedRate.response) return acceptedRate.response;
  const r2Available = Boolean(env.BUNDLES && typeof env.BUNDLES.put === "function");
  if (!r2Available) {
    if (!env.GOOGLE_APPS_SCRIPT_URL || !env.GOOGLE_APPS_SCRIPT_SECRET) {
      await releaseRateLimit(env, acceptedRate.reservation);
      return reject(
        "private_delivery_unavailable",
        503,
        "Private feedback delivery is temporarily unavailable.",
      );
    }
    if (upload.bundleBytes.byteLength > MAX_EMAIL_BUNDLE_BYTES) {
      await releaseRateLimit(env, acceptedRate.reservation);
      return reject(
        "email_bundle_too_large",
        413,
        "The private feedback bundle exceeds the email delivery limit.",
      );
    }
  }
  const submissionId = crypto.randomUUID();
  const objectKey = r2Available
    ? `feedback/${upload.caseId}/${upload.expectedSha}.zip`
    : `email:${upload.caseId}:${upload.expectedSha}`;
  const expiresUtc = new Date(
    Date.parse(receivedUtc) + FEEDBACK_RETENTION_DAYS * 24 * 60 * 60 * 1000,
  ).toISOString();
  let objectStored = false;
  let notificationSent = false;
  try {
    if (r2Available) {
      await env.BUNDLES.put(objectKey, upload.bundleBytes, {
        httpMetadata: { contentType: "application/zip" },
        customMetadata: { case_id: upload.caseId, bundle_sha256: upload.expectedSha },
      });
      objectStored = true;
    } else {
      notificationSent = await sendAppsScriptNotification(env, upload, receivedUtc);
      if (!notificationSent) throw new Error("private email delivery is unavailable");
    }
    await env.DB.prepare(
      "INSERT INTO feedback_cases(" +
        "case_id, bundle_sha256, submission_id, object_key, summary, reply_email, app_version, " +
        "created_utc, received_utc, expires_utc, status, notification_status, " +
        "notification_attempt_count, notification_last_attempt_utc, notification_sent_utc" +
      ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
    ).bind(
      upload.caseId,
      upload.expectedSha,
      submissionId,
      objectKey,
      SAFE_FEEDBACK_SUMMARY,
      upload.replyEmail,
      upload.appVersion,
      receivedUtc,
      receivedUtc,
      expiresUtc,
      r2Available ? "pending" : "sent",
      r2Available ? 0 : 1,
      r2Available ? null : receivedUtc,
      r2Available ? null : receivedUtc,
    ).run();
  } catch (error) {
    let raced = null;
    try {
      raced = await env.DB.prepare(
        "SELECT case_id, bundle_sha256, submission_id, received_utc, notification_status " +
        "FROM feedback_cases WHERE case_id = ?",
      ).bind(upload.caseId).first();
    } catch {
      raced = null;
    }
    if (raced && timingSafeEqual(String(raced.bundle_sha256), upload.expectedSha)) {
      const ownCommit = String(raced.submission_id || "") === submissionId;
      if (ownCommit && r2Available) {
        notificationSent = await attemptFeedbackNotification(
          env, upload, raced.received_utc,
        );
      } else if (!ownCommit) {
        await releaseRateLimit(env, acceptedRate.reservation);
      }
      return jsonResponse({
        ok: true,
        duplicate: !ownCommit,
        case_id: upload.caseId,
        bundle_sha256: upload.expectedSha,
        received_utc: raced.received_utc,
        notification_sent: notificationSent,
      }, ownCommit ? 201 : 200);
    }
    const rollbackResults = await Promise.allSettled([
      releaseRateLimit(env, acceptedRate.reservation),
      ...(objectStored ? [env.BUNDLES.delete(objectKey)] : []),
    ]);
    const quotaReleaseFailed = rollbackResults[0].status === "rejected";
    const objectDeleteFailed = objectStored && rollbackResults[1].status === "rejected";
    if (objectDeleteFailed) {
      await recordObjectCleanup(env, upload, objectKey, "intake_rollback_delete_failed", receivedUtc);
    }
    if (quotaReleaseFailed) {
      throw new Error("feedback rollback failed");
    }
    throw error;
  }
  if (r2Available) {
    notificationSent = await attemptFeedbackNotification(
      env, upload, receivedUtc,
    );
  }
  return jsonResponse({
    ok: true,
    duplicate: false,
    case_id: upload.caseId,
    bundle_sha256: upload.expectedSha,
    received_utc: receivedUtc,
    notification_sent: notificationSent,
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
  return `mailto:${encodeURIComponent(replyEmail)}?subject=${subject}`;
}

async function sendFeedbackNotification(env, upload, receivedUtc) {
  if (!env.FEEDBACK_EMAIL || !env.FEEDBACK_EMAIL_FROM || !env.SUPPORT_DESTINATION_EMAIL) {
    return false;
  }
  const from = normalizeEmail(env.FEEDBACK_EMAIL_FROM);
  const destination = normalizeEmail(env.SUPPORT_DESTINATION_EMAIL);
  if (!from) throw new Error("feedback notification sender is unavailable");
  if (!destination) throw new Error("feedback notification destination is unavailable");
  const lines = [
    "PeerBridge received a private feedback bundle.",
    "",
    `Reply address: ${upload.replyEmail || "not provided"}`,
    "",
    `Case: ${upload.caseId}`,
    `Bundle SHA-256: ${upload.expectedSha}`,
    `Received (UTC): ${receivedUtc}`,
    "",
    "Open the authenticated PeerBridge admin inbox to inspect the encrypted bundle.",
    "No caller-controlled summary, version text, bundle bytes, API keys, attachments, or diagnostics are included in this email.",
  ];
  await env.FEEDBACK_EMAIL.send({
    to: destination,
    from,
    subject: `PeerBridge feedback ${upload.caseId}`,
    text: lines.join("\n"),
    ...(upload.replyEmail ? { replyTo: upload.replyEmail } : {}),
  });
  return true;
}

async function sendAppsScriptNotification(env, upload, receivedUtc) {
  const endpointText = String(env.GOOGLE_APPS_SCRIPT_URL || "").trim();
  const secret = String(env.GOOGLE_APPS_SCRIPT_SECRET || "");
  if (!endpointText && !secret) return false;
  if (!endpointText || secret.length < 43 || secret.length > 256 || /[\u0000-\u0020\u007f]/u.test(secret)) {
    throw new Error("Apps Script notification configuration is incomplete");
  }
  const endpoint = normalizeAppsScriptEndpoint(endpointText);
  const timestamp = new Date().toISOString();
  const nonce = crypto.randomUUID().replaceAll("-", "");
  const notification = {
    schema: FEEDBACK_SCHEMA,
    case_id: upload.caseId,
    bundle_sha256: upload.expectedSha,
    bundle_base64: upload.bundleBase64,
    reply_email: upload.replyEmail || "",
    received_utc: receivedUtc,
    ingress_timestamp: timestamp,
    ingress_nonce: nonce,
  };
  const canonical = [
    notification.schema,
    notification.case_id,
    notification.bundle_sha256,
    notification.ingress_timestamp,
    notification.ingress_nonce,
    notification.reply_email,
    notification.received_utc,
  ].join("\n");
  notification.ingress_signature = await hmacHex(secret, canonical);
  const body = JSON.stringify(notification);
  let response = await fetch(endpoint, {
    method: "POST",
    headers: { "content-type": "application/json; charset=utf-8" },
    body,
    redirect: "manual",
  });
  if ([301, 302, 303, 307, 308].includes(response.status)) {
    const receiptUrl = normalizeAppsScriptReceiptRedirect(response.headers.get("location"));
    response = await fetch(receiptUrl, {
      method: "GET",
      headers: { accept: "application/json" },
      redirect: "error",
    });
  }
  const responseText = await response.text();
  if (!response.ok || responseText.length > 64 * 1024) {
    throw new Error("Apps Script notification request failed");
  }
  let receipt;
  try {
    receipt = JSON.parse(responseText);
  } catch {
    throw new Error("Apps Script notification receipt is invalid");
  }
  if (
    !receipt || receipt.ok !== true
    || receipt.case_id !== upload.caseId
    || !timingSafeEqual(String(receipt.bundle_sha256 || ""), upload.expectedSha)
  ) {
    throw new Error("Apps Script notification receipt does not match the feedback case");
  }
  return true;
}

async function notifyFeedbackWithoutAffectingReceipt(env, upload, receivedUtc) {
  try {
    if (env.GOOGLE_APPS_SCRIPT_URL || env.GOOGLE_APPS_SCRIPT_SECRET) {
      return await sendAppsScriptNotification(env, upload, receivedUtc);
    }
    return await sendFeedbackNotification(env, upload, receivedUtc);
  } catch (error) {
    console.error(JSON.stringify({
      event: "feedback_notification_failed",
      error_type: error instanceof TypeError ? "TypeError" : "Error",
    }));
    return false;
  }
}

async function claimNotificationAttempt(env, caseId, attemptedUtc) {
  const claimTokenSha256 = await sha256Hex(
    new TextEncoder().encode(crypto.randomUUID()),
  );
  const claimExpiresUtc = new Date(
    Date.parse(attemptedUtc) + NOTIFICATION_CLAIM_TTL_MS,
  ).toISOString();
  const result = await env.DB.prepare(
    "UPDATE feedback_cases SET notification_claim_token_sha256 = ?, " +
    "notification_claim_expires_utc = ? " +
    "WHERE case_id = ? AND notification_status IN ('pending', 'failed') " +
    "AND notification_attempt_count < ? " +
    "AND (notification_claim_token_sha256 IS NULL OR notification_claim_expires_utc <= ?)",
  ).bind(
    claimTokenSha256,
    claimExpiresUtc,
    caseId,
    MAX_NOTIFICATION_ATTEMPTS,
    attemptedUtc,
  ).run();
  return Number(result.meta?.changes || 0) === 1 ? claimTokenSha256 : null;
}

async function recordNotificationAttempt(
  env, caseId, claimTokenSha256, sent, attemptedUtc,
) {
  const result = await env.DB.prepare(
    "UPDATE feedback_cases SET notification_status = ?, " +
    "notification_attempt_count = notification_attempt_count + 1, " +
    "notification_last_attempt_utc = ?, " +
    "notification_sent_utc = CASE WHEN ? = 1 THEN COALESCE(notification_sent_utc, ?) " +
    "ELSE notification_sent_utc END, " +
    "notification_claim_token_sha256 = NULL, notification_claim_expires_utc = NULL " +
    "WHERE case_id = ? AND notification_status IN ('pending', 'failed') " +
    "AND notification_claim_token_sha256 = ?",
  ).bind(
    sent ? "sent" : "failed",
    attemptedUtc,
    sent ? 1 : 0,
    attemptedUtc,
    caseId,
    claimTokenSha256,
  ).run();
  if (Number(result.meta?.changes || 0) !== 1) {
    throw new Error("feedback notification claim was lost");
  }
}

async function attemptFeedbackNotification(env, upload, receivedUtc) {
  const attemptedUtc = new Date().toISOString();
  const claimTokenSha256 = await claimNotificationAttempt(
    env, upload.caseId, attemptedUtc,
  );
  if (!claimTokenSha256) return false;
  const sent = await notifyFeedbackWithoutAffectingReceipt(env, upload, receivedUtc);
  try {
    await recordNotificationAttempt(
      env, upload.caseId, claimTokenSha256, sent, attemptedUtc,
    );
  } catch {
    console.error(JSON.stringify({ event: "feedback_notification_state_update_failed" }));
  }
  return sent;
}

async function recordUnavailableNotificationAttempt(env, caseId, attemptedUtc) {
  const claimTokenSha256 = await claimNotificationAttempt(env, caseId, attemptedUtc);
  if (!claimTokenSha256) return;
  await recordNotificationAttempt(env, caseId, claimTokenSha256, false, attemptedUtc);
}

async function retryPendingFeedbackNotifications(env, now = new Date().toISOString()) {
  const rows = await env.DB.prepare(
    "SELECT case_id, bundle_sha256, object_key, reply_email, received_utc " +
    "FROM feedback_cases WHERE notification_status IN ('pending', 'failed') " +
    "AND notification_attempt_count < ? " +
    "AND (notification_claim_token_sha256 IS NULL OR notification_claim_expires_utc <= ?) " +
    "ORDER BY COALESCE(notification_last_attempt_utc, received_utc) ASC LIMIT ?",
  ).bind(MAX_NOTIFICATION_ATTEMPTS, now, MAX_NOTIFICATION_RETRY_CASES).all();
  let sentCount = 0;
  for (const row of rows.results || []) {
    try {
      if (String(row.object_key || "").startsWith("email:")) {
        await recordUnavailableNotificationAttempt(env, row.case_id, now);
        continue;
      }
      let bundleBase64 = "";
      if (env.GOOGLE_APPS_SCRIPT_URL || env.GOOGLE_APPS_SCRIPT_SECRET) {
        if (!env.BUNDLES || typeof env.BUNDLES.get !== "function") {
          await recordUnavailableNotificationAttempt(env, row.case_id, now);
          continue;
        }
        const object = await env.BUNDLES.get(row.object_key);
        if (!object) {
          await recordUnavailableNotificationAttempt(env, row.case_id, now);
          continue;
        }
        const bytes = new Uint8Array(await object.arrayBuffer());
        if (
          !bytes.byteLength || bytes.byteLength > MAX_EMAIL_BUNDLE_BYTES
          || !timingSafeEqual(await sha256Hex(bytes), String(row.bundle_sha256 || ""))
        ) {
          await recordUnavailableNotificationAttempt(env, row.case_id, now);
          continue;
        }
        bundleBase64 = encodeBase64(bytes);
      }
      const sent = await attemptFeedbackNotification(env, {
        caseId: row.case_id,
        expectedSha: row.bundle_sha256,
        replyEmail: row.reply_email || null,
        bundleBase64,
      }, row.received_utc);
      if (sent) sentCount += 1;
    } catch {
      try {
        await recordUnavailableNotificationAttempt(env, row.case_id, now);
      } catch {
        // The pending row remains durable for a later scheduled retry.
      }
      console.error(JSON.stringify({ event: "feedback_notification_retry_failed" }));
    }
  }
  return sentCount;
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
  if (String(row.object_key || "").startsWith("email:")) {
    return errorResponse(
      "bundle_delivered_by_email",
      410,
      "The private bundle was delivered by email and is not retained by the Worker.",
    );
  }
  if (!env.BUNDLES || typeof env.BUNDLES.get !== "function") {
    return errorResponse("bundle_missing", 503, "Feedback bundle storage is unavailable.");
  }
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

async function pruneExpiredRateLimits(env) {
  const cutoff = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 10);
  await env.DB.prepare("DELETE FROM rate_limits WHERE rate_day < ?")
    .bind(cutoff)
    .run();
}

async function pruneExpiredFeedback(env, now = new Date().toISOString()) {
  const rows = await env.DB.prepare(
    "SELECT case_id, object_key FROM feedback_cases " +
    "WHERE expires_utc IS NOT NULL AND expires_utc <= ? ORDER BY expires_utc ASC LIMIT ?",
  ).bind(now, MAX_RETENTION_PRUNE_CASES).all();
  let removed = 0;
  for (const row of rows.results || []) {
    try {
      const r2Backed = !String(row.object_key || "").startsWith("email:");
      if (r2Backed) {
        if (!env.BUNDLES || typeof env.BUNDLES.delete !== "function") {
          console.error(JSON.stringify({ event: "feedback_retention_r2_unavailable" }));
          continue;
        }
        await env.BUNDLES.delete(row.object_key);
      }
      const result = await env.DB.prepare(
        "DELETE FROM feedback_cases WHERE case_id = ? AND object_key = ? AND expires_utc <= ?",
      ).bind(row.case_id, row.object_key, now).run();
      removed += Number(result.meta?.changes || 0);
    } catch {
      console.error(JSON.stringify({ event: "feedback_retention_prune_failed" }));
    }
  }
  return removed;
}

async function pruneOrphanedFeedbackObjects(env, now = new Date().toISOString()) {
  if (!env.BUNDLES || typeof env.BUNDLES.delete !== "function") return 0;
  const rows = await env.DB.prepare(
    "SELECT object_key, case_id FROM feedback_object_cleanup " +
    "ORDER BY created_utc ASC LIMIT ?",
  ).bind(MAX_OBJECT_CLEANUP_RECORDS).all();
  let removed = 0;
  for (const row of rows.results || []) {
    try {
      const owner = await env.DB.prepare(
        "SELECT case_id FROM feedback_cases WHERE object_key = ?",
      ).bind(row.object_key).first();
      if (!owner) await env.BUNDLES.delete(row.object_key);
      const result = await env.DB.prepare(
        "DELETE FROM feedback_object_cleanup WHERE object_key = ?",
      ).bind(row.object_key).run();
      removed += Number(result.meta?.changes || 0);
    } catch {
      try {
        await env.DB.prepare(
          "UPDATE feedback_object_cleanup SET attempt_count = attempt_count + 1, updated_utc = ? " +
          "WHERE object_key = ?",
        ).bind(now, row.object_key).run();
      } catch {
        // The original cleanup row remains durable even if retry bookkeeping fails.
      }
      console.error(JSON.stringify({ event: "feedback_object_cleanup_failed" }));
    }
  }
  return removed;
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
      return jsonResponse({
        ok: true,
        service: "peerbridge-edge",
        bindings: {
          bundles: Boolean(
            env.BUNDLES
            && typeof env.BUNDLES.put === "function"
            && typeof env.BUNDLES.get === "function"
            && typeof env.BUNDLES.delete === "function"
          ),
        },
        retention: {
          d1_days: FEEDBACK_RETENTION_DAYS,
          r2_lifecycle_rule: {
            required: true,
            prefix: "feedback/",
            expiration_days: FEEDBACK_RETENTION_DAYS,
          },
        },
      });
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
    ctx.waitUntil(Promise.all([
      sendDigest(env),
      retryPendingFeedbackNotifications(env),
      pruneExpiredRateLimits(env),
      pruneExpiredFeedback(env),
      pruneOrphanedFeedbackObjects(env),
    ]));
  },
};

export const testing = {
  decodeBase64,
  normalizeEmail,
  normalizeHttpsUrl,
  normalizeAppsScriptEndpoint,
  attemptFeedbackNotification,
  retryPendingFeedbackNotifications,
  pruneOrphanedFeedbackObjects,
  pruneExpiredFeedback,
  pruneExpiredRateLimits,
  readJsonBody,
  recordObjectCleanup,
  sendFeedbackNotification,
  sendAppsScriptNotification,
  rateLimit,
  releaseRateLimit,
  timingSafeEqual,
  validateAnnouncement,
  attachmentPayloadIsValid,
  validateFeedbackUpload,
  validateZipBundle,
  validateZipBundleContents,
};
