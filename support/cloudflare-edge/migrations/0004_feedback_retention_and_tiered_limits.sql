-- Add bounded retention and separate anonymous-attempt limits from accepted-case
-- capacity. This migration is append-only and preserves every existing row.

ALTER TABLE feedback_cases ADD COLUMN expires_utc TEXT;

UPDATE feedback_cases
SET expires_utc = datetime(received_utc, '+30 days')
WHERE expires_utc IS NULL;

CREATE INDEX IF NOT EXISTS idx_feedback_cases_expiry
    ON feedback_cases(expires_utc ASC);

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_source_cap_v1_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'attempt-source:%' AND NEW.request_count > 20
BEGIN
    SELECT RAISE(ABORT, 'source attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_source_cap_v1_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'attempt-source:%' AND NEW.request_count > 20
BEGIN
    SELECT RAISE(ABORT, 'source attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_source_cap_v1_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'accepted-source:%' AND NEW.request_count > 5
BEGIN
    SELECT RAISE(ABORT, 'accepted source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_source_cap_v1_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'accepted-source:%' AND NEW.request_count > 5
BEGIN
    SELECT RAISE(ABORT, 'accepted source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_global_cap_v1_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'accepted-global' AND NEW.request_count > 100
BEGIN
    SELECT RAISE(ABORT, 'accepted global rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_accepted_global_cap_v1_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'accepted-global' AND NEW.request_count > 100
BEGIN
    SELECT RAISE(ABORT, 'accepted global rate limit exceeded');
END;
