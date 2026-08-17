-- Add stricter Alpha caps without dropping or rewriting existing feedback data.
-- Existing installations keep their earlier triggers; these additional triggers
-- enforce the lower bounds concurrently.

CREATE TRIGGER IF NOT EXISTS rate_limits_source_cap_alpha_v2_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key LIKE 'source:%' AND NEW.request_count > 5
BEGIN
    SELECT RAISE(ABORT, 'source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_source_cap_alpha_v2_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key LIKE 'source:%' AND NEW.request_count > 5
BEGIN
    SELECT RAISE(ABORT, 'source rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_global_cap_alpha_v2_insert
BEFORE INSERT ON rate_limits
WHEN NEW.rate_key = 'global' AND NEW.request_count > 100
BEGIN
    SELECT RAISE(ABORT, 'global rate limit exceeded');
END;

CREATE TRIGGER IF NOT EXISTS rate_limits_global_cap_alpha_v2_update
BEFORE UPDATE OF request_count ON rate_limits
WHEN NEW.rate_key = 'global' AND NEW.request_count > 100
BEGIN
    SELECT RAISE(ABORT, 'global rate limit exceeded');
END;
