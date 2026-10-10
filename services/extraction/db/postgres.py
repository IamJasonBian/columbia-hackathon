"""Stream mailbox extractions into PostgreSQL."""

from __future__ import annotations

import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from services.extraction.models import ExtractedDocument

PROJECT_ENV = Path(__file__).resolve().parents[3] / ".env"
REQUIRED_QUOTE_TABLES = frozenset({"rfq_quotes", "doc_lines.lines", "compliance"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailbox_messages (
    id BIGSERIAL PRIMARY KEY,
    mailbox TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL,
    subject TEXT,
    from_address TEXT,
    message_date TIMESTAMPTZ,
    kind TEXT NOT NULL,
    document JSONB NOT NULL,
    parsed JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (mailbox, gmail_message_id)
);

CREATE TABLE IF NOT EXISTS mailbox_poll_state (
    mailbox TEXT PRIMARY KEY,
    after_epoch BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RFQ → quote (normalized document lines + compliance)

CREATE TABLE IF NOT EXISTS rfq_quotes (
    id BIGSERIAL PRIMARY KEY,
    mailbox TEXT,
    gmail_message_id TEXT,
    customer_id TEXT,
    rfq_number TEXT,
    email_subject TEXT,
    source_message_date TIMESTAMPTZ,
    buyer_organization TEXT,
    workflow_status TEXT NOT NULL DEFAULT 'intake'
        CHECK (workflow_status IN (
            'intake', 'parsing', 'needs_clarification',
            'human_review', 'rejected', 'ready_to_send'
        )),
    reject_reason TEXT,
    has_attachments BOOLEAN NOT NULL DEFAULT false,
    parsed JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (mailbox, gmail_message_id)
);

-- Extracted document lines live in their own schema: doc_lines.lines.
CREATE SCHEMA IF NOT EXISTS doc_lines;

-- One-time move of the legacy line_items table; keeps rows, ids, and FKs.
DO $$
BEGIN
    IF to_regclass('doc_lines.lines') IS NULL AND to_regclass('line_items') IS NOT NULL THEN
        ALTER TABLE line_items SET SCHEMA doc_lines;
        ALTER TABLE doc_lines.line_items RENAME TO lines;
        ALTER SEQUENCE IF EXISTS doc_lines.line_items_id_seq RENAME TO lines_id_seq;
        ALTER INDEX IF EXISTS doc_lines.idx_line_items_quote RENAME TO idx_lines_quote;
        ALTER INDEX IF EXISTS doc_lines.idx_line_items_part_no RENAME TO idx_lines_part_no;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'compliance' AND column_name = 'line_item_id'
    ) THEN
        ALTER TABLE compliance RENAME COLUMN line_item_id TO line_id;
        ALTER INDEX IF EXISTS idx_compliance_line_item RENAME TO idx_compliance_line;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS doc_lines.lines (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE,
    line_seq INT NOT NULL,
    line_no INT,
    rfq_format TEXT NOT NULL CHECK (rfq_format IN ('numbered', 'spec_ref', 'description_part')),
    buyer_part_no TEXT,
    description TEXT NOT NULL,
    quantity NUMERIC,
    unit_of_measure TEXT,
    unit_price NUMERIC,
    delivery TEXT,
    is_option BOOLEAN NOT NULL DEFAULT false,
    required_qty_raw TEXT,
    source_attachment TEXT,
    source_page INT,
    source_table TEXT,
    raw_row JSONB NOT NULL DEFAULT '{}'::jsonb,
    match_score NUMERIC,
    quoted_unit_price NUMERIC,
    extended_price NUMERIC,
    market_benchmark_price NUMERIC,
    price_rationale TEXT,
    line_status TEXT NOT NULL DEFAULT 'parsed'
        CHECK (line_status IN (
            'pending', 'parsed', 'matched', 'priced',
            'ambiguous', 'unmatched', 'rejected'
        )),
    clarifying_question TEXT,
    availability_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (quote_id, line_seq)
);

CREATE INDEX IF NOT EXISTS idx_lines_part_no ON doc_lines.lines (buyer_part_no);

CREATE TABLE IF NOT EXISTS compliance (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE,
    line_id BIGINT REFERENCES doc_lines.lines(id) ON DELETE CASCADE,
    requirement_code TEXT NOT NULL,
    requirement_label TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'email_text',
    matched_pattern TEXT,
    source_text TEXT,
    status TEXT NOT NULL DEFAULT 'required'
        CHECK (status IN (
            'required', 'document_missing', 'satisfied', 'waived', 'send_to_buyer'
        )),
    required_document_type TEXT,
    attachment_path TEXT,
    buyer_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lines_quote ON doc_lines.lines (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_quote ON compliance (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_line ON compliance (line_id);

ALTER TABLE rfq_quotes ADD COLUMN IF NOT EXISTS customer_id TEXT;
ALTER TABLE compliance ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'email_text';
"""


def database_url_from_env() -> str | None:
    load_dotenv(PROJECT_ENV)
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


def _executemany(conn, sql: str, params: list[tuple[Any, ...]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(sql, params)


def _parse_message_date(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


class PostgresSink:
    """Upsert parsed messages and mailbox poll state in PostgreSQL."""

    def __init__(self, conninfo: str, *, connect_timeout: float | None = None):
        self.conninfo = conninfo
        self.connect_timeout = connect_timeout
        if self.connect_timeout is None:
            raw = os.environ.get("DATABASE_CONNECT_TIMEOUT", "15")
            try:
                self.connect_timeout = float(raw)
            except ValueError:
                self.connect_timeout = 15.0
        self._schema_ready = False

    def _connect(self):
        import psycopg

        return psycopg.connect(self.conninfo, connect_timeout=self.connect_timeout)

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return

        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()
        self._schema_ready = True

    def verify_quote_tables(self) -> set[str]:
        """Return required quote tables (schema-qualified where needed) that do not exist."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM unnest(%s::text[]) AS t(name) WHERE to_regclass(name) IS NULL",
                (sorted(REQUIRED_QUOTE_TABLES),),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def write(
        self,
        mailbox: str,
        gmail_message_id: str,
        doc: ExtractedDocument,
        *,
        parsed: dict[str, Any] | None = None,
    ) -> None:
        """Insert or replace one parsed message."""
        from psycopg.types.json import Json

        self.ensure_schema()
        meta = doc.metadata or {}
        message_date = _parse_message_date(meta.get("date"))
        document = doc.model_dump(mode="json")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mailbox_messages (
                    mailbox, gmail_message_id, subject, from_address,
                    message_date, kind, document, parsed
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mailbox, gmail_message_id) DO UPDATE SET
                    subject = EXCLUDED.subject,
                    from_address = EXCLUDED.from_address,
                    message_date = EXCLUDED.message_date,
                    kind = EXCLUDED.kind,
                    document = EXCLUDED.document,
                    parsed = COALESCE(EXCLUDED.parsed, mailbox_messages.parsed),
                    ingested_at = now()
                """,
                (
                    mailbox,
                    gmail_message_id,
                    meta.get("subject"),
                    meta.get("from"),
                    message_date,
                    doc.kind.value,
                    Json(document),
                    Json(parsed) if parsed is not None else None,
                ),
            )

    def load_poll_state(self, mailbox: str, lookback_seconds: int) -> tuple[int, list[str]]:
        """Create/read a mailbox cursor and return IDs already persisted."""
        import time

        self.ensure_schema()
        initial_after = int(time.time()) - lookback_seconds
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO mailbox_poll_state (mailbox, after_epoch)
                    VALUES (%s, %s)
                    ON CONFLICT (mailbox) DO UPDATE SET mailbox = EXCLUDED.mailbox
                    RETURNING after_epoch
                    """,
                    (mailbox, initial_after),
                ).fetchone()
                seen = conn.execute(
                    """
                    SELECT gmail_message_id
                    FROM mailbox_messages
                    WHERE mailbox = %s
                    ORDER BY ingested_at, id
                    """,
                    (mailbox,),
                ).fetchall()
        return int(row[0]), [str(item[0]) for item in seen]

    def save_poll_state(self, mailbox: str, after_epoch: int) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mailbox_poll_state (mailbox, after_epoch)
                VALUES (%s, %s)
                ON CONFLICT (mailbox) DO UPDATE SET
                    after_epoch = EXCLUDED.after_epoch,
                    updated_at = now()
                """,
                (mailbox, after_epoch),
            )

    def get_message(self, mailbox: str, gmail_message_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT document
                FROM mailbox_messages
                WHERE mailbox = %s AND gmail_message_id = %s
                """,
                (mailbox, gmail_message_id),
            ).fetchone()
        return row[0] if row else None

    def ping(self) -> str:
        with self._connect() as conn:
            return conn.execute("SELECT current_database()").fetchone()[0]
