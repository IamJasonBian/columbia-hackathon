from __future__ import annotations

from pathlib import Path

from extraction_service.db.sqlite_queue import ServiceQueue
from extraction_service.models import ExtractedDocument
from extraction_service.quote.task_enqueue import RfqTaskEnqueuer, enqueue_rfq_line_items_task


def test_enqueue_only_no_postgres(tmp_path: Path):
    q = ServiceQueue(tmp_path / "q.db")
    doc = ExtractedDocument(
        filename="m.eml",
        kind="email",
        metadata={"subject": "RFQ-MED-1"},
    )
    job_id, task_id = enqueue_rfq_line_items_task(
        q, mailbox="a@b.com", gmail_message_id="mid1", doc=doc
    )
    job = q.get(job_id)
    assert job is not None
    assert job.status == "pending"
    assert job.job_type == "rfq_line_items"
    assert task_id.startswith("rfq-")
    assert job.payload["email_subject"] == "RFQ-MED-1"

    enqueuer = RfqTaskEnqueuer("a@b.com", queue=q)
    tid = enqueuer("mid1", doc)
    assert tid.startswith("rfq-")
