"""Visible trust records with live source-state verification."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Literal

from .bridge import Bridge, BridgeError, stable_sha256, utc_now
from .secret_scan import contains_secret


TRUST_STAGES = (
    "claim",
    "execution",
    "test",
    "proof",
    "review",
    "decision",
    "disagreement",
    "recheck",
    "completion",
)
EVIDENCE_STAGES = frozenset(
    {
        "test",
        "proof",
        "review",
        "decision",
        "disagreement",
        "recheck",
        "completion",
    }
)
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


class TrustTimelineError(RuntimeError):
    """A trust claim is unbound, stale, or internally inconsistent."""


def _source_key(binding: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(binding["path"]),
        int(binding["bytes"]),
        str(binding["sha256"]),
    )


def _trust_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    source_bindings = (
        row["source_bindings"]
        if isinstance(row, dict) and "source_bindings" in row
        else json.loads(row["source_bindings_json"])
    )
    related = (
        row["related_record_ids"]
        if isinstance(row, dict) and "related_record_ids" in row
        else json.loads(row["related_record_ids_json"])
    )
    return {
        "scope": row["scope"],
        "record_id": row["record_id"],
        "task_id": row["task_id"],
        "actor": row["actor"],
        "stage": row["stage"],
        "statement": row["statement"],
        "source_bindings": source_bindings,
        "related_record_ids": related,
        "created_utc": row["created_utc"],
    }


class TrustTimeline:
    """Append trust claims, then compute freshness against the live project."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.scope = bridge.scope

    @staticmethod
    def _statement(value: Any) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 8_000:
            raise TrustTimelineError("trust statement is invalid or too large")
        if contains_secret(text):
            raise TrustTimelineError("trust statement contains credential-like data")
        return text

    def _bindings(self, artifact_paths: Iterable[str]) -> list[dict[str, Any]]:
        try:
            normalized = self.bridge._clean_artifacts(list(artifact_paths))
        except BridgeError as exc:
            raise TrustTimelineError(str(exc)) from exc
        if len(normalized) > 100:
            raise TrustTimelineError("trust record exceeds 100 source bindings")
        if not normalized:
            return []
        try:
            hashed_paths = self.bridge._hash_proof_files(sorted(set(normalized)))
        except BridgeError as exc:
            raise TrustTimelineError(str(exc)) from exc
        bindings = []
        for relative, hashed in hashed_paths.items():
            bindings.append(
                {
                    "path": relative,
                    "bytes": int(hashed["bytes"]),
                    "sha256": str(hashed["sha256"]),
                }
            )
        return bindings

    def _row(self, connection: sqlite3.Connection, record_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM trust_records WHERE scope=? AND record_id=?",
            (self.scope, record_id),
        ).fetchone()
        if row is None:
            raise TrustTimelineError("trust record does not exist")
        if stable_sha256(_trust_payload(row)) != row["trust_sha256"]:
            raise TrustTimelineError("trust record SHA-256 does not match")
        return row

    def record(
        self,
        *,
        task_id: str,
        stage: Literal[
            "claim",
            "execution",
            "test",
            "proof",
            "review",
            "decision",
            "disagreement",
            "recheck",
            "completion",
        ],
        statement: str,
        artifact_paths: Iterable[str] = (),
        related_record_ids: Iterable[str] = (),
        record_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not SAFE_ID.fullmatch(task_id):
            raise TrustTimelineError("task id is invalid")
        if stage not in TRUST_STAGES:
            raise TrustTimelineError("trust stage is invalid")
        if stage == "decision" and self.bridge.agent_id != "human-operator":
            raise TrustTimelineError(
                "only human-operator may record a Trust Timeline decision"
            )
        statement = self._statement(statement)
        bindings = self._bindings(artifact_paths)
        related = []
        for value in related_record_ids:
            text = str(value or "").strip()
            if not SAFE_ID.fullmatch(text):
                raise TrustTimelineError("related trust record id is invalid")
            if text not in related:
                related.append(text)
        if len(related) > 100:
            raise TrustTimelineError("trust record has too many related records")
        if stage in EVIDENCE_STAGES and not bindings and not related:
            raise TrustTimelineError("evidence trust stage requires a source or related record")
        record_id = str(record_id or uuid.uuid4().hex)
        if not SAFE_ID.fullmatch(record_id):
            raise TrustTimelineError("trust record id is invalid")
        created = utc_now()
        payload = {
            "scope": self.scope,
            "record_id": record_id,
            "task_id": task_id,
            "actor": self.bridge.agent_id,
            "stage": stage,
            "statement": statement,
            "source_bindings": bindings,
            "related_record_ids": related,
            "created_utc": created,
        }
        digest = stable_sha256(payload)
        with self.bridge._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for related_id in related:
                related_row = self._row(connection, related_id)
                if str(related_row["task_id"]) != task_id:
                    raise TrustTimelineError("related trust record belongs to another task")
            try:
                connection.execute(
                    """INSERT INTO trust_records(
                        scope, record_id, task_id, actor, stage, statement,
                        source_bindings_json, related_record_ids_json, created_utc,
                        trust_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.scope,
                        record_id,
                        task_id,
                        self.bridge.agent_id,
                        stage,
                        statement,
                        json.dumps(
                            bindings,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(related, separators=(",", ":")),
                        created,
                        digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TrustTimelineError("trust record id already exists") from exc
            event = self.bridge._event(
                connection,
                f"trust.{stage}",
                {
                    "record_id": record_id,
                    "task_id": task_id,
                    "stage": stage,
                    "source_count": len(bindings),
                    "related_count": len(related),
                    "trust_sha256": digest,
                },
                task_id,
            )
        return {
            **payload,
            "trust_sha256": digest,
            "audit_chain_sha256": event["chain_sha256"],
        }

    def _freshness(
        self,
        binding: dict[str, Any],
        live_hashes: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        path = str(binding["path"])
        hashed = live_hashes.get(path)
        if hashed is None:
            return {
                **binding,
                "stale": True,
                "stale_reason": "source_missing",
                "live_bytes": None,
                "live_sha256": None,
            }
        live_bytes = int(hashed["bytes"])
        live_sha = str(hashed["sha256"])
        stale = live_bytes != int(binding["bytes"]) or live_sha != str(binding["sha256"])
        return {
            **binding,
            "stale": stale,
            "stale_reason": "source_changed" if stale else None,
            "live_bytes": live_bytes,
            "live_sha256": live_sha,
        }

    def timeline(self, task_id: str) -> list[dict[str, Any]]:
        task_id = str(task_id or "").strip()
        if not SAFE_ID.fullmatch(task_id):
            raise TrustTimelineError("task id is invalid")
        with self.bridge._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM trust_records WHERE scope=? AND task_id=?
                    ORDER BY created_utc, rowid LIMIT 501""",
                (self.scope, task_id),
            ).fetchall()
            if len(rows) > 500:
                raise TrustTimelineError("trust timeline exceeds the record limit")
            payloads = [
                _trust_payload(self._row(connection, str(row["record_id"])))
                for row in rows
            ]
            paths = list(
                dict.fromkeys(
                    str(binding["path"])
                    for item in payloads
                    for binding in item["source_bindings"]
                )
            )
            if len(paths) > 100:
                raise TrustTimelineError("trust timeline exceeds the source binding limit")
            existing = [
                path for path in paths if self.bridge._resolve_path(path).is_file()
            ]
            try:
                live_hashes = self.bridge._hash_proof_files(existing) if existing else {}
            except BridgeError as exc:
                raise TrustTimelineError(str(exc)) from exc
            result = []
            for row, item in zip(rows, payloads, strict=True):
                live_bindings = [
                    self._freshness(binding, live_hashes)
                    for binding in item["source_bindings"]
                ]
                item.update(
                    {
                        "source_bindings": live_bindings,
                        "stale": any(binding["stale"] for binding in live_bindings),
                        "trust_sha256": str(row["trust_sha256"]),
                    }
                )
                result.append(item)
        return result

    def record_disagreement(
        self,
        *,
        task_id: str,
        statement: str,
        evidence_record_ids: Iterable[str],
    ) -> dict[str, Any]:
        related = list(dict.fromkeys(str(value) for value in evidence_record_ids))
        if len(related) < 2:
            raise TrustTimelineError("disagreement requires at least two evidence records")
        live = {row["record_id"]: row for row in self.timeline(task_id)}
        if any(live.get(record_id, {}).get("stale", True) for record_id in related):
            raise TrustTimelineError("disagreement evidence is stale")
        with self.bridge._connect() as connection:
            source_paths = []
            for record_id in related:
                row = self._row(connection, record_id)
                if row["stage"] not in {"test", "proof", "review", "decision"}:
                    raise TrustTimelineError("disagreement reference is not evidence")
                bindings = json.loads(row["source_bindings_json"])
                if not bindings:
                    raise TrustTimelineError(
                        "disagreement evidence lacks an exact source binding"
                    )
                source_paths.extend(item["path"] for item in bindings)
        return self.record(
            task_id=task_id,
            stage="disagreement",
            statement=statement,
            artifact_paths=source_paths,
            related_record_ids=related,
        )

    def recheck(
        self, record_id: str, *, statement: str
    ) -> dict[str, Any]:
        with self.bridge._connect() as connection:
            row = self._row(connection, str(record_id))
            source_paths = [
                item["path"] for item in json.loads(row["source_bindings_json"])
            ]
            task_id = str(row["task_id"])
        if not source_paths:
            raise TrustTimelineError("trust record has no bounded source to recheck")
        return self.record(
            task_id=task_id,
            stage="recheck",
            statement=statement,
            artifact_paths=source_paths,
            related_record_ids=[record_id],
        )

    def record_completion(
        self,
        *,
        task_id: str,
        statement: str,
        evidence_record_ids: Iterable[str],
    ) -> dict[str, Any]:
        related = list(dict.fromkeys(str(value) for value in evidence_record_ids))
        with self.bridge._connect() as connection:
            rows = [self._row(connection, record_id) for record_id in related]
        stages = {str(row["stage"]) for row in rows}
        required = {"test", "proof", "review", "decision"}
        if not required.issubset(stages):
            raise TrustTimelineError(
                "completion requires test, proof, review, and human decision records"
            )
        binding_sets = [
            {
                _source_key(binding)
                for binding in json.loads(str(row["source_bindings_json"]))
            }
            for row in rows
            if str(row["stage"]) in {"test", "proof", "review", "decision"}
        ]
        if not binding_sets or not binding_sets[0] or any(
            bindings != binding_sets[0] for bindings in binding_sets[1:]
        ):
            raise TrustTimelineError("completion evidence does not bind one exact source state")
        source_paths = [path for path, _bytes, _sha in sorted(binding_sets[0])]
        live_records = {row["record_id"]: row for row in self.timeline(task_id)}
        if any(live_records.get(record_id, {}).get("stale", True) for record_id in related):
            raise TrustTimelineError("completion evidence is stale")
        return self.record(
            task_id=task_id,
            stage="completion",
            statement=statement,
            artifact_paths=source_paths,
            related_record_ids=related,
        )


__all__ = [
    "EVIDENCE_STAGES",
    "TRUST_STAGES",
    "TrustTimeline",
    "TrustTimelineError",
]
