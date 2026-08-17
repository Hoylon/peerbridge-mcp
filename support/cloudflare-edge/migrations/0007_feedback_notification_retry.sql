-- Preserve every accepted feedback receipt while making private notification
-- delivery retryable. Existing rows are intentionally marked legacy_unknown so
-- deployment cannot resend historical mail unexpectedly.

ALTER TABLE feedback_cases ADD COLUMN notification_status TEXT NOT NULL DEFAULT 'legacy_unknown'
    CHECK (notification_status IN ('legacy_unknown', 'pending', 'failed', 'sent'));
ALTER TABLE feedback_cases ADD COLUMN notification_attempt_count INTEGER NOT NULL DEFAULT 0
    CHECK (notification_attempt_count >= 0);
ALTER TABLE feedback_cases ADD COLUMN notification_last_attempt_utc TEXT;
ALTER TABLE feedback_cases ADD COLUMN notification_sent_utc TEXT;

CREATE INDEX IF NOT EXISTS idx_feedback_cases_notification_retry
    ON feedback_cases(notification_status, notification_last_attempt_utc, received_utc ASC);
