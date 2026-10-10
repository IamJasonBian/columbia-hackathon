"""Enqueue RFQ pipeline tasks in SQLite (poller → worker, no Postgres writes here)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from extraction_service.db.sqlite_queue import ServiceQueue, default_queue_path
from extraction_service.models import ExtractedDocument


def new_task_id(prefix: str = "rfq") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def enqueue_rfq_line_items_task(
    queue: ServiceQueue,
    *,
    mailbox: str,
    gmail_message_id: str,
    doc: ExtractedDocument | None = None,
    source: str = "poller",
) -> tuple[int, str]:
    """Record work in SQLite only; worker hot-loads the document before DB writes."""
    task_id = new_task_id("rfq")
    meta = (doc.metadata or {}) if doc else {}
    job = queue.enqueue(
        "rfq_line_items",
        idempotency_key=f"line_items:{mailbox}:{gmail_message_id}",
        mailbox=mailbox.lower(),
        gmail_message_id=gmail_message_id,
        stage="intake",
        payload={
            "task_id": task_id,
            "source": source,
            "email_subject": meta.get("subject"),
        },
    )
    return job.id, task_id


@dataclass
class RfqTaskEnqueuer:
    """Poller hook: enqueue SQLite task after extract (no quote DB writes)."""

    mailbox: str
    queue: ServiceQueue | None = None

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = ServiceQueue(default_queue_path())

    def __call__(self, message_id: str, doc: ExtractedDocument) -> str:
        _, task_id = enqueue_rfq_line_items_task(
            self.queue,
            mailbox=self.mailbox,
            gmail_message_id=message_id,
            doc=doc,
            source="poller",
        )
        return task_id

    @classmethod
    def for_mailbox(cls, mailbox: str) -> RfqTaskEnqueuer:
        return cls(mailbox=mailbox.lower())
