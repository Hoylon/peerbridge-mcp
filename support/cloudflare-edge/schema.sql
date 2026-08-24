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
    expires_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    digested_utc TEXT,
    replied_utc TEXT,
    notification_status TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK (notification_status IN ('legacy_unknown', 'pending', 'failed', 'sent')),
    notification_attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (notification_attempt_count >= 0),
    notification_last_attempt_utc TEXT,
    notification_sent_utc TEXT,
    notification_claim_token_sha256 TEXT
        CHECK (notification_claim_token_sha256 IS NULL OR length(notification_claim_token_sha256) = 64),
    notification_claim_expires_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_cases_received
    ON feedback_cases(received_utc DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_cases_status
    ON feedback_cases(status, received_utc DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_cases_expiry
    ON feedback_cases(expires_utc ASC);
CREATE INDEX IF NOT EXISTS idx_feedback_cases_notification_retry
    ON feedback_cases(notification_status, notification_last_attempt_utc, received_utc ASC);
CREATE INDEX IF NOT EXISTS idx_feedback_cases_notification_claim
    ON feedback_cases(notification_status, notification_claim_expires_utc, received_utc ASC);

CREATE TABLE IF NOT EXISTS rate_limits (
    rate_key TEXT NOT NULL,
    rate_day TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (rate_key, rate_day)
);

-- D1 executes batched statements transactionally. These triggers make both the
-- per-source and global increments fail closed under concurrent submissions.
CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_source_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'attempt-source:%' AND NEW.request_count > 20
BEGIN
    SELECT RAISE(ABORT, 'source attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_source_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'attempt-source:%' AND NEW.request_count > 20
BEGIN
    SELECT RAISE(ABORT, 'source attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_global_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'attempt-global' AND NEW.request_count > 500
BEGIN
    SELECT RAISE(ABORT, 'global attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_global_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'attempt-global' AND NEW.request_count > 500
BEGIN
    SELECT RAISE(ABORT, 'global attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_source_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'accepted-source:%' AND NEW.request_count > 5
BEGIN
    SELECT RAISE(ABORT, 'accepted source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_source_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'accepted-source:%' AND NEW.request_count > 5
BEGIN
    SELECT RAISE(ABORT, 'accepted source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_global_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'accepted-global' AND NEW.request_count > 100
BEGIN
    SELECT RAISE(ABORT, 'accepted global rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_global_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'accepted-global' AND NEW.request_count > 100
BEGIN
    SELECT RAISE(ABORT, 'accepted global rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_announcement_source_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'announcement-source:%' AND NEW.request_count > 240
BEGIN
    SELECT RAISE(ABORT, 'source announcement rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_announcement_source_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'announcement-source:%' AND NEW.request_count > 240
BEGIN
    SELECT RAISE(ABORT, 'source announcement rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_announcement_global_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'announcement-global' AND NEW.request_count > 20000
BEGIN
    SELECT RAISE(ABORT, 'global announcement rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_announcement_global_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'announcement-global' AND NEW.request_count > 20000
BEGIN
    SELECT RAISE(ABORT, 'global announcement rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_anonymous_notification_cap_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'notification-anonymous-global' AND NEW.request_count > 20
BEGIN
    SELECT RAISE(ABORT, 'anonymous notification capacity exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_anonymous_notification_cap_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'notification-anonymous-global' AND NEW.request_count > 20
BEGIN
    SELECT RAISE(ABORT, 'anonymous notification capacity exceeded');
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
