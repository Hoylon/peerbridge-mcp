-- Bound anonymous announcement reads and preserve notification capacity for
-- authenticated operational traffic. Apply once after migration 0008.

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
