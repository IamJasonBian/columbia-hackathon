"""Persist RFQ line_items to Postgres (called by pipeline worker, not the poller)."""

from __future__ import annotations

import logging
import re

from extraction_service.models import ExtractedDocument
from extraction_service.db.sqlite_queue import ServiceQueue
from extraction_service.quote.compliance_scan import scan_compliance_text
from extraction_service.quote.models import ComplianceRecord, LineItemRecord, RfqQuoteHeader
from extraction_service.quote.repository import QuoteRepository
from extraction_service.quote.rfq_line_extract import extract_lines_from_document, rfq_number_from_subject

logger = logging.getLogger(__name__)

_CERT_RE = re.compile(r"(?i)required certifications:\s*(.+?)(?:\n|$)")


def persist_extracted_document(
    message_id: str,
    doc: ExtractedDocument,
    *,
    mailbox: str,
    repo: QuoteRepository,
    queue: ServiceQueue | None = None,
    job_id: int | None = None,
) -> int:
    """Parse tables and upsert ``rfq_quotes`` + ``line_items`` (+ compliance)."""
    try:
        if queue and job_id is not None:
            queue.set_stage(job_id, "parse_lines")

        meta = doc.metadata or {}
        subject = meta.get("subject")
        lines = extract_lines_from_document(doc)
        items = [LineItemRecord.from_extracted(row) for row in lines]

        text_blob = doc.text or ""
        for att in doc.attachments:
            text_blob += "\n" + (att.text or "")
        compliance = scan_compliance_text(text_blob)
        cert_match = _CERT_RE.search(text_blob)
        if cert_match:
            compliance.append(
                ComplianceRecord(
                    requirement_code="buyer_cert",
                    requirement_label=cert_match.group(1).strip(),
                    source_text=cert_match.group(0),
                    status="required",
                )
            )

        if queue and job_id is not None:
            queue.set_stage(job_id, "persist")

        header = RfqQuoteHeader(
            mailbox=mailbox,
            gmail_message_id=message_id,
            rfq_number=rfq_number_from_subject(subject),
            email_subject=subject,
            source_message_date=meta.get("date"),
            has_attachments=bool(doc.attachments),
            workflow_status="parsing" if items else "human_review",
            parsed={"line_count": len(items), "source": "worker"},
        )
        quote_id = repo.save_quote_bundle(header, items, compliance)

        if queue and job_id is not None:
            queue.set_postgres_quote_id(job_id, quote_id)
            queue.complete(job_id)

        logger.info(
            "rfq persist %s -> quote_id=%s lines=%s",
            message_id,
            quote_id,
            len(items),
        )
        return quote_id
    except Exception:
        if queue and job_id is not None:
            self_fail = queue.fail(job_id, "rfq line_items persist failed")
            logger.debug("job %s status after fail: %s", job_id, self_fail.status)
        raise


# Backward-compatible alias for scripts/tests
ingest_extracted_document = persist_extracted_document
