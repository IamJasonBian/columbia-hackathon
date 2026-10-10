"""Nicrron email classification and customer compliance jobs."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from services.extraction.db.postgres import PostgresSink, database_url_from_env
from services.extraction.db.sqlite_queue import ServiceQueue, default_queue_path
from services.extraction.nicrron import NicrronExtractionError, assess_email_compliance
from services.extraction.quote.customer_compliance import (
    CustomerComplianceMapping,
    load_customer_compliance,
)
from services.extraction.quote.document_loader import load_extracted_document
from services.extraction.quote.models import ComplianceRecord
from services.extraction.quote.repository import QuoteRepository

logger = logging.getLogger(__name__)


def enqueue_classification(
    queue: ServiceQueue,
    *,
    mailbox: str,
    gmail_message_id: str,
    quote_id: int | None = None,
) -> int:
    task_id = f"cls-{uuid.uuid4().hex[:12]}"
    job = queue.enqueue(
        "nicrron_classify",
        idempotency_key=f"classify:{mailbox}:{gmail_message_id}",
        mailbox=mailbox,
        gmail_message_id=gmail_message_id,
        stage="intake",
        payload={
            "task_id": task_id,
            "quote_id": quote_id,
            "source": "pipeline",
        },
    )
    return job.id


@dataclass
class ClassificationWorker:
    queue: ServiceQueue
    sink: PostgresSink | None
    pulls_root: Path | None = None
    customer_mapping: CustomerComplianceMapping | None = None
    repository: QuoteRepository | None = None
    instructions: str | None = None
    model: str | None = None
    poll_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> ClassificationWorker:
        url = database_url_from_env()
        sink = PostgresSink(url) if url else None
        if sink:
            sink.ensure_schema()
        return cls(
            queue=ServiceQueue(default_queue_path()),
            sink=sink,
            pulls_root=Path(os.environ.get("PULLS_DIR", "app_storage/pulls")),
            customer_mapping=load_customer_compliance(),
            repository=QuoteRepository(sink) if sink else None,
            instructions=os.environ.get("NICRRON_CLASSIFY_INSTRUCTIONS"),
            model=os.environ.get("NICRRON_MODEL"),
            poll_seconds=float(os.environ.get("CLASSIFY_POLL_SECONDS", "2")),
        )

    def process_one(self) -> bool:
        job = self.queue.claim_next(job_types=["nicrron_classify"])
        if job is None:
            return False
        return self.process_one_from_job(job)

    def process_one_from_job(self, job) -> bool:
        task_id = (job.payload or {}).get("task_id") or f"job-{job.id}"
        try:
            self.queue.set_stage(job.id, "parse_lines")
            doc = load_extracted_document(
                job.mailbox or "",
                job.gmail_message_id or "",
                sink=self.sink,
                pulls_root=self.pulls_root,
            )
            mapping = self.customer_mapping or load_customer_compliance()
            customer = mapping.resolve((doc.metadata or {}).get("from"))
            requirements = customer.compliance_documents if customer else []

            self.queue.set_stage(job.id, "compliance")
            try:
                result = assess_email_compliance(
                    doc,
                    requirements,
                    instructions=self.instructions,
                    model=self.model,
                )
            except NicrronExtractionError as exc:
                payload = dict(job.payload)
                payload["classification"] = None
                self.queue.update_payload(job.id, payload)
                self.queue.complete(job.id)
                logger.warning(
                    "classification unavailable task_id=%s message=%s: %s",
                    task_id,
                    job.gmail_message_id,
                    exc,
                )
                return True
            body = result.model_dump(mode="json")
            body["task_id"] = task_id
            body["customer_id"] = customer.customer_id if customer else None
            body["customer_mapped"] = customer is not None

            quote_id = job.payload.get("quote_id")
            repo = self.repository or (QuoteRepository(self.sink) if self.sink else None)
            if repo is not None and quote_id is not None:
                repo.update_classification(
                    int(quote_id),
                    customer_id=customer.customer_id if customer else None,
                    classification=body,
                    human_review=customer is None,
                )
                if customer is not None:
                    lines = repo.get_lines(int(quote_id))
                    by_code = {item.code: item for item in customer.compliance_documents}
                    records: list[ComplianceRecord] = []
                    for line in lines:
                        for assessment in result.compliance_documents:
                            requirement = by_code[assessment.requirement_code]
                            records.append(
                                ComplianceRecord(
                                    line_id=int(line["id"]),
                                    requirement_code=requirement.code,
                                    requirement_label=requirement.document_name,
                                    source="customer_mapping",
                                    source_text=assessment.rationale,
                                    status=assessment.status,
                                    required_document_type=requirement.code,
                                    attachment_path=assessment.attachment_path,
                                    buyer_message=(
                                        None
                                        if assessment.status == "satisfied"
                                        else f"Please provide {requirement.document_name}."
                                    ),
                                )
                            )
                    repo.replace_compliance_source(
                        int(quote_id),
                        "customer_mapping",
                        records,
                    )
                else:
                    repo.replace_compliance_source(
                        int(quote_id),
                        "customer_mapping",
                        [],
                    )

            self.queue.set_stage(job.id, "finalize")
            payload = dict(job.payload)
            payload["classification"] = body
            self.queue.update_payload(job.id, payload)
            self.queue.complete(job.id)
            logger.info(
                "classification task_id=%s message=%s label=%s",
                task_id,
                job.gmail_message_id,
                result.email_label,
            )
        except Exception as exc:
            self.queue.fail(job.id, str(exc))
            logger.exception("classification failed task_id=%s", task_id)
        return True

    def run_forever(self, stop: callable | None = None) -> None:
        import time

        while not (stop and stop()):
            try:
                if not self.process_one():
                    time.sleep(self.poll_seconds)
            except Exception:
                logger.exception("classification worker tick failed")
                time.sleep(self.poll_seconds)
