from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "support" / "cloudflare-edge" / "migrations"


def test_cloudflare_migration_chain_preserves_legacy_rows_and_enforces_caps(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-feedback.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE feedback_cases (
                case_id TEXT PRIMARY KEY,
                bundle_sha256 TEXT NOT NULL,
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
            CREATE TABLE rate_limits (
                rate_key TEXT NOT NULL,
                rate_day TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                updated_utc TEXT NOT NULL,
                PRIMARY KEY (rate_key, rate_day)
            );
            INSERT INTO feedback_cases(
                case_id, bundle_sha256, object_key, summary,
                created_utc, received_utc
            ) VALUES (
                '0123456789abcdef0123456789abcdef',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'feedback/legacy/bundle.zip',
                'legacy summary',
                '2026-08-01T00:00:00Z',
                '2026-08-01T00:00:00Z'
            );
            """
        )
        migration_paths = sorted(MIGRATIONS.glob("*.sql"))
        assert [path.name for path in migration_paths] == [
            "0002_feedback_submission_id.sql",
            "0003_tighten_feedback_rate_limits.sql",
            "0004_feedback_retention_and_tiered_limits.sql",
            "0005_global_feedback_attempt_cap.sql",
            "0006_feedback_object_cleanup.sql",
            "0007_feedback_notification_retry.sql",
            "0008_feedback_notification_claim.sql",
            "0009_external_capacity_and_announcement_reads.sql",
        ]
        for path in migration_paths:
            connection.executescript(path.read_text(encoding="utf-8"))

        row = connection.execute(
            "SELECT submission_id, expires_utc, notification_status, "
            "notification_attempt_count, notification_claim_token_sha256, "
            "notification_claim_expires_utc FROM feedback_cases"
        ).fetchone()
        assert row == (
            "legacy-0123456789abcdef0123456789abcdef",
            "2026-08-31 00:00:00",
            "legacy_unknown",
            0,
            None,
            None,
        )
        connection.execute(
            "INSERT INTO rate_limits VALUES (?, ?, ?, ?)",
            ("attempt-global", "2026-08-17", 500, "2026-08-17T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="global attempt rate limit"):
            connection.execute(
                "UPDATE rate_limits SET request_count=501 "
                "WHERE rate_key='attempt-global' AND rate_day='2026-08-17'"
            )
        for rate_key, maximum, message in (
            ("announcement-source:fixture", 240, "source announcement rate limit"),
            ("announcement-global", 20_000, "global announcement rate limit"),
            (
                "notification-anonymous-global",
                20,
                "anonymous notification capacity",
            ),
        ):
            connection.execute(
                "INSERT INTO rate_limits VALUES (?, ?, ?, ?)",
                (rate_key, "2026-08-18", maximum, "2026-08-18T00:00:00Z"),
            )
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(
                    "UPDATE rate_limits SET request_count=? "
                    "WHERE rate_key=? AND rate_day='2026-08-18'",
                    (maximum + 1, rate_key),
                )
        cleanup_objects = dict(
            connection.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE name IN (?, ?)",
                (
                    "feedback_object_cleanup",
                    "idx_feedback_object_cleanup_created",
                ),
            ).fetchall()
        )
        assert cleanup_objects == {
            "feedback_object_cleanup": "table",
            "idx_feedback_object_cleanup_created": "index",
        }
