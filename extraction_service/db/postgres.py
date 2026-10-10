"""Stream mailbox extractions into PostgreSQL."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from extraction_service.models import ExtractedDocument, ExtractedTable

PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"
REQUIRED_QUOTE_TABLES = frozenset({"rfq_quotes", "line_items", "compliance"})

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

CREATE TABLE IF NOT EXISTS mailbox_table_rows (
    id BIGSERIAL PRIMARY KEY,
    mailbox TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL,
    attachment_path TEXT NOT NULL DEFAULT '',
    table_name TEXT,
    row_index INT NOT NULL,
    headers JSONB NOT NULL DEFAULT '[]'::jsonb,
    record JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mailbox_table_rows_message
    ON mailbox_table_rows (mailbox, gmail_message_id);

CREATE TABLE IF NOT EXISTS mailbox_poll_state (
    mailbox TEXT PRIMARY KEY,
    after_epoch BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TRACK 2 | RudCo | RFQ → quote (normalized lines, catalog match, compliance)

CREATE TABLE IF NOT EXISTS skumaster (
    id BIGSERIAL PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    buyer_part_no TEXT,
    name TEXT NOT NULL,
    description TEXT,
    material TEXT,
    grade TEXT,
    spec_standard TEXT,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    unit_of_measure TEXT,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skumaster_material_grade
    ON skumaster (material, grade)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_skumaster_buyer_part_no
    ON skumaster (buyer_part_no)
    WHERE buyer_part_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS catalog (
    id BIGSERIAL PRIMARY KEY,
    skumaster_id BIGINT REFERENCES skumaster(id) ON DELETE SET NULL,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    material TEXT,
    grade TEXT,
    spec_standard TEXT,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    unit_of_measure TEXT,
    list_price NUMERIC,
    currency TEXT NOT NULL DEFAULT 'USD',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS item_items (
    id BIGSERIAL PRIMARY KEY,
    catalog_id BIGINT NOT NULL REFERENCES catalog(id) ON DELETE CASCADE,
    supplier_sku TEXT,
    description TEXT NOT NULL,
    market_benchmark_price NUMERIC,
    last_price_updated_at TIMESTAMPTZ,
    in_stock BOOLEAN,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (catalog_id, supplier_sku)
);

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

CREATE TABLE IF NOT EXISTS line_items (
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
    skumaster_id BIGINT REFERENCES skumaster(id) ON DELETE SET NULL,
    catalog_id BIGINT REFERENCES catalog(id) ON DELETE SET NULL,
    item_item_id BIGINT REFERENCES item_items(id) ON DELETE SET NULL,
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

CREATE INDEX IF NOT EXISTS idx_line_items_part_no ON line_items (buyer_part_no);

CREATE TABLE IF NOT EXISTS compliance (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE,
    line_item_id BIGINT REFERENCES line_items(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_line_items_quote ON line_items (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_quote ON compliance (quote_id);
CREATE INDEX IF NOT EXISTS idx_compliance_line_item ON compliance (line_item_id);

ALTER TABLE rfq_quotes ADD COLUMN IF NOT EXISTS customer_id TEXT;
ALTER TABLE compliance ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'email_text';
"""


def database_url_from_env() -> str | None:
    load_dotenv(PROJECT_ENV)
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


def iter_table_records(
    doc: ExtractedDocument, attachment_path: str = ""
) -> Iterator[tuple[str, str | None, int, list[str], dict[str, Any]]]:
    """Yield (attachment_path, table_name, row_index, headers, record) for every table row."""
    for table in doc.tables:
        yield from _table_rows(attachment_path, table)
    for att in doc.attachments:
        child = f"{attachment_path}/{att.filename}" if attachment_path else att.filename
        yield from iter_table_records(att, child)


def _table_rows(
    attachment_path: str, table: ExtractedTable
) -> Iterator[tuple[str, str | None, int, list[str], dict[str, Any]]]:
    headers = list(table.headers)
    for row_index, row in enumerate(table.rows):
        if headers:
            record = dict(zip(headers, row))
        else:
            record = {str(i): cell for i, cell in enumerate(row)}
        yield attachment_path, table.name, row_index, headers, record


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
    """Upsert parsed messages and stream table rows into PostgreSQL."""

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
        """Return required quote tables missing from the current PostgreSQL schema."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                """,
                (list(REQUIRED_QUOTE_TABLES),),
            ).fetchall()
        present = {str(row[0]) for row in rows}
        return set(REQUIRED_QUOTE_TABLES) - present

    def write(
        self,
        mailbox: str,
        gmail_message_id: str,
        doc: ExtractedDocument,
        *,
        parsed: dict[str, Any] | None = None,
    ) -> None:
        """Insert or replace one message and its table rows."""
        from psycopg.types.json import Json

        self.ensure_schema()
        meta = doc.metadata or {}
        message_date = _parse_message_date(meta.get("date"))
        document = doc.model_dump(mode="json")

        with self._connect() as conn:
            with conn.transaction():
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
                conn.execute(
                    "DELETE FROM mailbox_table_rows WHERE mailbox = %s AND gmail_message_id = %s",
                    (mailbox, gmail_message_id),
                )
                rows = [
                    (
                        mailbox,
                        gmail_message_id,
                        attachment_path,
                        table_name,
                        row_index,
                        Json(headers),
                        Json(record),
                    )
                    for attachment_path, table_name, row_index, headers, record in iter_table_records(doc)
                ]
                if rows:
                    _executemany(
                        conn,
                        """
                        INSERT INTO mailbox_table_rows (
                            mailbox, gmail_message_id, attachment_path,
                            table_name, row_index, headers, record
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        rows,
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

    def list_mailboxes(self) -> list[str]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT mailbox FROM mailbox_poll_state
                UNION
                SELECT DISTINCT mailbox FROM mailbox_messages
                ORDER BY mailbox
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def poll_state(self, mailbox: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT after_epoch, updated_at FROM mailbox_poll_state WHERE mailbox = %s",
                (mailbox,),
            ).fetchone()
            if row is None:
                return None
            counts = conn.execute(
                """
                SELECT count(*), max(ingested_at)
                FROM mailbox_messages
                WHERE mailbox = %s
                """,
                (mailbox,),
            ).fetchone()
        return {
            "after_epoch": int(row[0]),
            "processed_count": int(counts[0]),
            "last_processed_at": counts[1].isoformat() if counts[1] else None,
            "updated_at": row[1].isoformat(),
        }

    def list_messages(self, mailbox: str, limit: int = 100) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT gmail_message_id, message_date, from_address, subject, document
                FROM mailbox_messages
                WHERE mailbox = %s
                ORDER BY message_date DESC NULLS LAST, ingested_at DESC
                LIMIT %s
                """,
                (mailbox, limit),
            ).fetchall()
        return [
            {
                "message_id": str(row[0]),
                "date": row[1].isoformat() if row[1] else None,
                "from": row[2],
                "subject": row[3],
                "attachments": [
                    {
                        "filename": item.get("filename"),
                        "kind": item.get("kind"),
                        "chars": len(item.get("text") or ""),
                    }
                    for item in (row[4] or {}).get("attachments", [])
                ],
            }
            for row in rows
        ]

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
