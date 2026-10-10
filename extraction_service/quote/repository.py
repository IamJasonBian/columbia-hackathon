"""Persist parsed RFQ lines and compliance flags to PostgreSQL."""

from __future__ import annotations

from typing import Any

from extraction_service.db.postgres import PostgresSink, _executemany
from extraction_service.quote.models import ComplianceRecord, LineItemRecord, RfqQuoteHeader


class QuoteRepository:
    """Upsert ``rfq_quotes``, replace ``line_items``, replace ``compliance`` for one quote."""

    def __init__(self, sink: PostgresSink):
        self.sink = sink

    def upsert_quote(self, header: RfqQuoteHeader) -> int:
        from psycopg.types.json import Json

        self.sink.ensure_schema()
        with self.sink._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO rfq_quotes (
                    mailbox, gmail_message_id, customer_id, rfq_number, email_subject,
                    source_message_date, buyer_organization, workflow_status,
                    reject_reason, has_attachments, parsed, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (mailbox, gmail_message_id) DO UPDATE SET
                    customer_id = COALESCE(EXCLUDED.customer_id, rfq_quotes.customer_id),
                    rfq_number = COALESCE(EXCLUDED.rfq_number, rfq_quotes.rfq_number),
                    email_subject = COALESCE(EXCLUDED.email_subject, rfq_quotes.email_subject),
                    source_message_date = COALESCE(
                        EXCLUDED.source_message_date, rfq_quotes.source_message_date
                    ),
                    buyer_organization = COALESCE(
                        EXCLUDED.buyer_organization, rfq_quotes.buyer_organization
                    ),
                    workflow_status = EXCLUDED.workflow_status,
                    reject_reason = EXCLUDED.reject_reason,
                    has_attachments = EXCLUDED.has_attachments,
                    parsed = COALESCE(EXCLUDED.parsed, rfq_quotes.parsed),
                    updated_at = now()
                RETURNING id
                """,
                (
                    header.mailbox,
                    header.gmail_message_id,
                    header.customer_id,
                    header.rfq_number,
                    header.email_subject,
                    header.source_message_date,
                    header.buyer_organization,
                    header.workflow_status,
                    header.reject_reason,
                    header.has_attachments,
                    Json(header.parsed) if header.parsed is not None else None,
                ),
            ).fetchone()
            conn.commit()
            return int(row[0])

    def replace_line_items(self, quote_id: int, items: list[LineItemRecord]) -> None:
        from psycopg.types.json import Json

        self.sink.ensure_schema()
        with self.sink._connect() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM line_items WHERE quote_id = %s", (quote_id,))
                if not items:
                    return
                _executemany(
                    conn,
                    """
                    INSERT INTO line_items (
                        quote_id, line_seq, line_no, rfq_format, buyer_part_no, description,
                        quantity, unit_of_measure, unit_price, delivery, is_option,
                        required_qty_raw, source_attachment, source_page, source_table,
                        raw_row, skumaster_id, catalog_id, item_item_id, match_score,
                        quoted_unit_price, extended_price, market_benchmark_price,
                        price_rationale, line_status, clarifying_question, availability_note
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    [
                        (
                            quote_id,
                            item.line_seq,
                            item.line_no,
                            item.rfq_format,
                            item.buyer_part_no,
                            item.description,
                            item.quantity,
                            item.unit_of_measure,
                            item.unit_price,
                            item.delivery,
                            item.is_option,
                            item.required_qty_raw,
                            item.source_attachment,
                            item.source_page,
                            item.source_table,
                            Json(item.raw_row),
                            item.skumaster_id,
                            item.catalog_id,
                            item.item_item_id,
                            item.match_score,
                            item.quoted_unit_price,
                            item.extended_price,
                            item.market_benchmark_price,
                            item.price_rationale,
                            item.line_status,
                            item.clarifying_question,
                            item.availability_note,
                        )
                        for item in items
                    ],
                )

    def replace_compliance(self, quote_id: int, records: list[ComplianceRecord]) -> None:
        self.sink.ensure_schema()
        with self.sink._connect() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM compliance WHERE quote_id = %s", (quote_id,))
                self._insert_compliance(conn, quote_id, records)

    def replace_compliance_source(
        self,
        quote_id: int,
        source: str,
        records: list[ComplianceRecord],
    ) -> None:
        """Replace one enrichment source without deleting regex/manual compliance rows."""
        self.sink.ensure_schema()
        with self.sink._connect() as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM compliance WHERE quote_id = %s AND source = %s",
                    (quote_id, source),
                )
                self._insert_compliance(conn, quote_id, records)

    @staticmethod
    def _insert_compliance(conn: Any, quote_id: int, records: list[ComplianceRecord]) -> None:
        if not records:
            return
        _executemany(
            conn,
            """
            INSERT INTO compliance (
                quote_id, line_item_id, requirement_code, requirement_label, source,
                matched_pattern, source_text, status, required_document_type,
                attachment_path, buyer_message
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    quote_id,
                    rec.line_item_id,
                    rec.requirement_code,
                    rec.requirement_label,
                    rec.source,
                    rec.matched_pattern,
                    rec.source_text,
                    rec.status,
                    rec.required_document_type,
                    rec.attachment_path,
                    rec.buyer_message,
                )
                for rec in records
            ],
        )

    def update_classification(
        self,
        quote_id: int,
        *,
        customer_id: str | None,
        classification: dict[str, Any],
        human_review: bool = False,
    ) -> None:
        """Merge model output into the quote and optionally route it to human review."""
        from psycopg.types.json import Json

        self.sink.ensure_schema()
        with self.sink._connect() as conn:
            conn.execute(
                """
                UPDATE rfq_quotes
                SET customer_id = %s,
                    parsed = COALESCE(parsed, '{}'::jsonb)
                        || jsonb_build_object('classification', %s::jsonb),
                    workflow_status = CASE
                        WHEN %s THEN 'human_review'
                        ELSE workflow_status
                    END,
                    updated_at = now()
                WHERE id = %s
                """,
                (customer_id, Json(classification), human_review, quote_id),
            )
            conn.commit()

    def save_quote_bundle(
        self,
        header: RfqQuoteHeader,
        items: list[LineItemRecord],
        compliance: list[ComplianceRecord],
    ) -> int:
        quote_id = self.upsert_quote(header)
        self.replace_line_items(quote_id, items)
        self.replace_compliance(quote_id, compliance)
        return quote_id

    def get_line_items(self, quote_id: int) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        self.sink.ensure_schema()
        with self.sink._connect() as conn:
            conn.row_factory = dict_row
            rows = conn.execute(
                """
                SELECT id, line_seq, line_no, rfq_format, buyer_part_no, description,
                       quantity, unit_of_measure, delivery, is_option, line_status
                FROM line_items
                WHERE quote_id = %s
                ORDER BY line_seq
                """,
                (quote_id,),
            ).fetchall()
        return list(rows)
