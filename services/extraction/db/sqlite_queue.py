"""Local SQLite queue for RFQ→quote service jobs (offline-safe, single-host)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

JobStatus = Literal["pending", "running", "succeeded", "failed", "dead"]
QuoteStage = Literal[
    "intake",
    "parse_lines",
    "match_catalog",
    "price",
    "compliance",
    "finalize",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    mailbox TEXT,
    gmail_message_id TEXT,
    postgres_quote_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'dead')),
    stage TEXT NOT NULL DEFAULT 'intake'
        CHECK (stage IN (
            'intake', 'parse_lines', 'match_catalog', 'price', 'compliance', 'finalize'
        )),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_after TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_service_jobs_poll
    ON service_jobs (status, run_after);

CREATE TABLE IF NOT EXISTS service_job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES service_jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_service_job_events_job
    ON service_job_events (job_id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_queue_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "app_storage" / "rfq" / "service_queue.db"


@dataclass
class ServiceJob:
    id: int
    job_type: str
    idempotency_key: str | None
    mailbox: str | None
    gmail_message_id: str | None
    postgres_quote_id: int | None
    status: JobStatus
    stage: QuoteStage
    attempts: int
    max_attempts: int
    last_error: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str
    run_after: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ServiceJob:
        return cls(
            id=row["id"],
            job_type=row["job_type"],
            idempotency_key=row["idempotency_key"],
            mailbox=row["mailbox"],
            gmail_message_id=row["gmail_message_id"],
            postgres_quote_id=row["postgres_quote_id"],
            status=row["status"],
            stage=row["stage"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            last_error=row["last_error"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            run_after=row["run_after"],
        )


class ServiceQueue:
    """Durable job queue stored in SQLite."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or default_queue_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def enqueue(
        self,
        job_type: str,
        *,
        idempotency_key: str | None = None,
        mailbox: str | None = None,
        gmail_message_id: str | None = None,
        stage: QuoteStage = "intake",
        payload: dict[str, Any] | None = None,
        run_after: datetime | None = None,
        max_attempts: int = 5,
    ) -> ServiceJob:
        now = _utc_now()
        run = (run_after or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
        body = json.dumps(payload or {})
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO service_jobs (
                        job_type, idempotency_key, mailbox, gmail_message_id,
                        stage, max_attempts, payload_json, created_at, updated_at, run_after
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_type,
                        idempotency_key,
                        mailbox,
                        gmail_message_id,
                        stage,
                        max_attempts,
                        body,
                        now,
                        now,
                        run,
                    ),
                )
                job_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                row = conn.execute(
                    "SELECT id FROM service_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is None:
                    raise
                job_id = int(row["id"])
            conn.commit()
        job = self.get(job_id)
        assert job is not None
        self._log_event(job_id, "enqueued", {"stage": stage})
        return job

    def get(self, job_id: int) -> ServiceJob | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM service_jobs WHERE id = ?", (job_id,)).fetchone()
        return ServiceJob.from_row(row) if row else None

    def claim_next(self, job_types: list[str] | None = None) -> ServiceJob | None:
        """Claim the oldest runnable pending job (single-worker safe)."""
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if job_types:
                placeholders = ",".join("?" for _ in job_types)
                row = conn.execute(
                    f"""
                    SELECT * FROM service_jobs
                    WHERE status = 'pending'
                      AND run_after <= ?
                      AND job_type IN ({placeholders})
                    ORDER BY run_after ASC, id ASC
                    LIMIT 1
                    """,
                    (now, *job_types),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM service_jobs
                    WHERE status = 'pending' AND run_after <= ?
                    ORDER BY run_after ASC, id ASC
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
            if row is None:
                conn.commit()
                return None
            job_id = row["id"]
            conn.execute(
                """
                UPDATE service_jobs
                SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM service_jobs WHERE id = ?", (job_id,)).fetchone()
        job = ServiceJob.from_row(row)
        self._log_event(job.id, "claimed", {"attempt": job.attempts})
        return job

    def set_stage(self, job_id: int, stage: QuoteStage) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE service_jobs SET stage = ?, updated_at = ? WHERE id = ?",
                (stage, now, job_id),
            )
            conn.commit()
        self._log_event(job_id, "stage", {"stage": stage})

    def set_postgres_quote_id(self, job_id: int, quote_id: int) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE service_jobs
                SET postgres_quote_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (quote_id, now, job_id),
            )
            conn.commit()
        self._log_event(job_id, "linked_quote", {"postgres_quote_id": quote_id})

    def update_payload(self, job_id: int, payload: dict[str, Any]) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE service_jobs SET payload_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload), now, job_id),
            )
            conn.commit()
        self._log_event(job_id, "payload_updated", {})

    def complete(self, job_id: int) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE service_jobs
                SET status = 'succeeded', stage = 'finalize', last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            conn.commit()
        self._log_event(job_id, "completed", {})

    def fail(self, job_id: int, error: str, *, retry_delay_seconds: int = 60) -> ServiceJob:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM service_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempts = row["attempts"]
            max_attempts = row["max_attempts"]
            if attempts >= max_attempts:
                status: JobStatus = "dead"
                run_after = now
            else:
                status = "pending"
                run_after = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat()
            conn.execute(
                """
                UPDATE service_jobs
                SET status = ?, last_error = ?, run_after = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error[:4000], run_after, now, job_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM service_jobs WHERE id = ?", (job_id,)).fetchone()
        job = ServiceJob.from_row(row)
        self._log_event(job_id, "failed", {"error": error, "status": job.status})
        return job

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[ServiceJob]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM service_jobs
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM service_jobs
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [ServiceJob.from_row(r) for r in rows]

    def _log_event(self, job_id: int, event_type: str, detail: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO service_job_events (job_id, event_type, detail_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, event_type, json.dumps(detail), _utc_now()),
            )
            conn.commit()
