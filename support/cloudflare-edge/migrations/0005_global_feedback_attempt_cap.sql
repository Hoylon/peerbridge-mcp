-- Bound aggregate anonymous work before JSON/ZIP parsing even when attackers
-- rotate source addresses. Existing rows and earlier per-source limits remain.

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_global_cap_v1_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'attempt-global' AND NEW.request_count > 500
BEGIN
    SELECT RAISE(ABORT, 'global attempt rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_attempt_global_cap_v1_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'attempt-global' AND NEW.request_count > 500
BEGIN
    SELECT RAISE(ABORT, 'global attempt rate limit exceeded');
END;
