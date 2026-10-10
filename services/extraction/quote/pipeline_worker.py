"""Process SQLite RFQ tasks: hot-load latest extract → Postgres → optional Nicrron."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from services.extraction.db.postgres import PostgresSink, database_url_from_env
from services.extraction.db.sqlite_queue import ServiceQueue, default_queue_path
from services.extraction.quote.classification_worker import ClassificationWorker, enqueue_classification
from services.extraction.quote.poller_ingest import persist_extracted_document
from services.extraction.quote.repository import QuoteRepository
from services.extraction.quote.document_loader import load_extracted_document

logger = logging.getLogger(__name__)

_ENABLE_CLASSIFY = os.environ.get("ENABLE_CLASSIFICATION_PIPELINE", "1") != "0"
_PIPELINE_JOB_TYPES = ["rfq_line_items", "nicrron_classify"]


@dataclass
class PipelineWorker:
    queue: ServiceQueue
    sink: PostgresSink
    pulls_root: Path | None = None
    poll_seconds: float = 2.0
    _classifier: ClassificationWorker | None = None

    @classmethod
    def from_env(cls) -> PipelineWorker:
        url = database_url_from_env()
        if not url:
            raise RuntimeError("DATABASE_URL is required for the pipeline worker")
        sink = PostgresSink(url)
        sink.ensure_schema()
        queue = ServiceQueue(default_queue_path())
        worker = cls(
            queue=queue,
            sink=sink,
            pulls_root=Path(os.environ.get("PULLS_DIR", "app_storage/pulls")),
            poll_seconds=float(os.environ.get("PIPELINE_POLL_SECONDS", "2")),
        )
        worker._classifier = ClassificationWorker(
            queue=queue,
            sink=sink,
            pulls_root=worker.pulls_root,
        )
        return worker

    def process_one(self) -> bool:
        job = self.queue.claim_next(job_types=_PIPELINE_JOB_TYPES)
        if job is None:
            return False

        if job.job_type == "nicrron_classify":
            assert self._classifier is not None
            return self._classifier.process_one_from_job(job)

        if job.job_type != "rfq_line_items":
            self.queue.fail(job.id, f"unknown job_type {job.job_type!r}")
            return True

        task_id = (job.payload or {}).get("task_id") or f"job-{job.id}"
        try:
            self.queue.set_stage(job.id, "parse_lines")
            doc = load_extracted_document(
                job.mailbox or "",
                job.gmail_message_id or "",
                sink=self.sink,
                pulls_root=self.pulls_root,
            )
            self.queue.set_stage(job.id, "persist")
            repo = QuoteRepository(self.sink)
            quote_id = persist_extracted_document(
                job.gmail_message_id or "",
                doc,
                mailbox=job.mailbox or "",
                repo=repo,
                queue=self.queue,
                job_id=job.id,
            )
            if _ENABLE_CLASSIFY:
                enqueue_classification(
                    self.queue,
                    mailbox=job.mailbox or "",
                    gmail_message_id=job.gmail_message_id or "",
                    quote_id=quote_id,
                )
            logger.info(
                "pipeline task_id=%s message=%s quote_id=%s",
                task_id,
                job.gmail_message_id,
                quote_id,
            )
        except Exception as exc:
            self.queue.fail(job.id, str(exc))
            logger.exception("rfq_line_items failed task_id=%s", task_id)
        return True

    def run_forever(self, stop: callable | None = None) -> None:
        import time

        while not (stop and stop()):
            try:
                if not self.process_one():
                    time.sleep(self.poll_seconds)
            except Exception:
                logger.exception("pipeline worker tick failed")
                time.sleep(self.poll_seconds)
