"""Enqueue RFQ quote work locally; worker stages write Postgres line_items + compliance."""

from __future__ import annotations

from typing import Any

from extraction_service.db.postgres import PostgresSink, database_url_from_env
from extraction_service.db.sqlite_queue import ServiceQueue
from extraction_service.quote.compliance_scan import scan_compliance_text
from extraction_service.quote.models import LineItemRecord, RfqQuoteHeader
from extraction_service.quote.repository import QuoteRepository


def enqueue_rfq_quote(
    *,
    mailbox: str,
    gmail_message_id: str,
    payload: dict[str, Any] | None = None,
    queue: ServiceQueue | None = None,
) -> int:
    """Idempotent local job for one mailbox message (SQLite only)."""
    from extraction_service.quote.task_enqueue import enqueue_rfq_line_items_task

    q = queue or ServiceQueue()
    job_id, _task_id = enqueue_rfq_line_items_task(
        q,
        mailbox=mailbox,
        gmail_message_id=gmail_message_id,
        source="legacy_enqueue_rfq_quote",
    )
    return job_id


def process_one_rfq_job(queue: ServiceQueue | None = None) -> bool:
    """Run one queued RFQ job through parse → compliance persist."""
    q = queue or ServiceQueue()
    job = q.claim_next(job_types=["rfq_quote"])
    if job is None:
        return False

    url = database_url_from_env()
    if not url:
        q.fail(job.id, "DATABASE_URL not configured")
        return True

    try:
        payload = job.payload
        q.set_stage(job.id, "parse_lines")
        lines_raw = payload.get("line_items") or []
        items: list[LineItemRecord] = []
        for idx, row in enumerate(lines_raw, start=1):
            if isinstance(row, dict):
                row.setdefault("line_seq", idx)
                row.setdefault("description", row.get("raw_description", ""))
                items.append(LineItemRecord.from_extracted(row))
            elif isinstance(row, str):
                items.append(
                    LineItemRecord(
                        line_seq=idx,
                        line_no=idx,
                        description=row,
                        line_status="parsed",
                    )
                )

        q.set_stage(job.id, "compliance")
        text_blob = payload.get("compliance_text") or payload.get("rfq_text") or ""
        compliance = scan_compliance_text(str(text_blob))

        header = RfqQuoteHeader(
            mailbox=job.mailbox,
            gmail_message_id=job.gmail_message_id,
            rfq_number=payload.get("rfq_number"),
            email_subject=payload.get("email_subject"),
            workflow_status=payload.get("workflow_status") or "parsing",
            has_attachments=bool(payload.get("has_attachments")),
            parsed=payload.get("parsed"),
        )

        repo = QuoteRepository(PostgresSink(url))
        quote_id = repo.save_quote_bundle(header, items, compliance)
        q.set_postgres_quote_id(job.id, quote_id)
        q.complete(job.id)
    except Exception as exc:
        q.fail(job.id, str(exc))
    return True
