-- Ensure duplicate intake requests and scheduled retries cannot deliver the
-- same private notification concurrently. Only a SHA-256 claim fingerprint is
-- stored; an expired claim can be recovered by a later scheduled run.

ALTER TABLE feedback_cases ADD COLUMN notification_claim_token_sha256 TEXT
    CHECK (notification_claim_token_sha256 IS NULL OR length(notification_claim_token_sha256) = 64);
ALTER TABLE feedback_cases ADD COLUMN notification_claim_expires_utc TEXT;

CREATE INDEX IF NOT EXISTS idx_feedback_cases_notification_claim
    ON feedback_cases(notification_status, notification_claim_expires_utc, received_utc ASC);
