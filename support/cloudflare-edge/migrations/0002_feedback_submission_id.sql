ALTER TABLE feedback_cases ADD COLUMN submission_id TEXT;

UPDATE feedback_cases
SET submission_id = 'legacy-' || case_id
WHERE submission_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_cases_submission_id
    ON feedback_cases(submission_id);
