CREATE TABLE IF NOT EXISTS feedback_cases (
    case_id TEXT PRIMARY KEY,
    bundle_sha256 TEXT NOT NULL,
    submission_id TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    reply_email TEXT,
    app_version TEXT,
    created_utc TEXT NOT NULL,
    received_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    digested_utc TEXT,
    replied_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_cases_received
    ON feedback_cases(received_utc DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_cases_status
    ON feedback_cases(status, received_utc DESC);

CREATE TABLE IF NOT EXISTS rate_limits (
    rate_key TEXT NOT NULL,
    rate_day TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (rate_key, rate_day)
);

-- D1 executes batched statements transactionally. These triggers make both the
-- per-source and global increments fail closed under concurrent submissions.
CREATE TRIGGER IF NOT EXISTS rate_limits_source_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'source:%' AND NEW.request_count > 50
BEGIN
    SELECT RAISE(ABORT, 'source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_source_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'source:%' AND NEW.request_count > 50
BEGIN
    SELECT RAISE(ABORT, 'source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_global_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'global' AND NEW.request_count > 5000
BEGIN
    SELECT RAISE(ABORT, 'global rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_global_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'global' AND NEW.request_count > 5000
BEGIN
    SELECT RAISE(ABORT, 'global rate limit exceeded');
END;

CREATE TABLE IF NOT EXISTS announcements (
    announcement_id TEXT NOT NULL,
    locale TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    severity TEXT NOT NULL,
    link_url TEXT,
    published_utc TEXT NOT NULL,
    expires_utc TEXT,
    created_utc TEXT NOT NULL,
    PRIMARY KEY (announcement_id, locale)
);

CREATE INDEX IF NOT EXISTS idx_announcements_locale_published
    ON announcements(locale, published_utc DESC);
