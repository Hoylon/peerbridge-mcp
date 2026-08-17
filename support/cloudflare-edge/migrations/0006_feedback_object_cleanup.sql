-- Retain durable retry state when an intake rollback cannot delete its R2 object.
-- The scheduled Worker verifies that no accepted case owns the key before deletion.

CREATE TABLE IF NOT EXISTS feedback_object_cleanup (
    object_key TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    bundle_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_feedback_object_cleanup_created
    ON feedback_object_cleanup(created_utc ASC);
