#!/usr/bin/env python3
"""HTTP task API + optional in-process worker (prefer ``bootstrap/worker.py`` in prod).

Poller enqueues SQLite tasks only; run ``bootstrap/worker.py`` (or gunicorn/uvicorn
with reload) separately so DB writes use the latest code.

Usage:
    export DATABASE_URL=...
    export WORKER_IN_PROCESS=0   # default: API only, worker is external
    python server.py
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("server")

app = FastAPI(title="RFQ task server", version="0.2.0")

_worker = None
_stop = threading.Event()


class TaskView(BaseModel):
    task_id: str
    job_id: int
    job_type: str
    status: str
    stage: str
    mailbox: str | None
    gmail_message_id: str | None
    postgres_quote_id: int | None = None
    payload: dict[str, Any] | None = None
    last_error: str | None = None


def _task_view(job) -> TaskView:
    payload = job.payload or {}
    task_id = payload.get("task_id") or f"job-{job.id}"
    return TaskView(
        task_id=task_id,
        job_id=job.id,
        job_type=job.job_type,
        status=job.status,
        stage=job.stage,
        mailbox=job.mailbox,
        gmail_message_id=job.gmail_message_id,
        postgres_quote_id=job.postgres_quote_id,
        payload=payload,
        last_error=job.last_error,
    )


@app.on_event("startup")
def _startup() -> None:
    global _worker
    if os.environ.get("WORKER_IN_PROCESS", "0") != "1":
        logger.info("WORKER_IN_PROCESS!=1 — run bootstrap/worker.py for processing")
        return

    from extraction_service.quote.pipeline_worker import PipelineWorker

    _worker = PipelineWorker.from_env()

    def _loop() -> None:
        assert _worker is not None
        logger.info("in-process pipeline worker started")
        _worker.run_forever(stop=_stop.is_set)

    threading.Thread(target=_loop, name="pipeline-worker", daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    _stop.set()


@app.get("/health")
def health() -> dict[str, Any]:
    from extraction_service.db.sqlite_queue import ServiceQueue, default_queue_path

    queue = ServiceQueue(default_queue_path())
    pending = len([j for j in queue.list_jobs(limit=500, status="pending")])
    return {
        "status": "ok",
        "database_configured": bool(os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")),
        "nicrron_configured": bool(os.environ.get("NICRRON_API_KEY")),
        "worker_in_process": os.environ.get("WORKER_IN_PROCESS", "0") == "1",
        "worker_alive": not _stop.is_set(),
        "sqlite_pending_jobs": pending,
    }


@app.get("/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str) -> TaskView:
    from extraction_service.db.sqlite_queue import ServiceQueue, default_queue_path

    queue = ServiceQueue(default_queue_path())
    for job in queue.list_jobs(limit=1000):
        payload = job.payload or {}
        if payload.get("task_id") == task_id or f"job-{job.id}" == task_id:
            return _task_view(job)
    raise HTTPException(404, "task not found")


@app.get("/tasks")
def list_tasks(
    limit: int = 50,
    status: str | None = None,
    job_type: str | None = None,
) -> list[TaskView]:
    from extraction_service.db.sqlite_queue import ServiceQueue, default_queue_path

    queue = ServiceQueue(default_queue_path())
    jobs = queue.list_jobs(limit=limit, status=status)  # type: ignore[arg-type]
    out = [_task_view(j) for j in jobs]
    if job_type:
        out = [t for t in out if t.job_type == job_type]
    return out


@app.post("/tasks/run-once")
def run_once() -> dict[str, Any]:
    from extraction_service.quote.pipeline_worker import PipelineWorker

    worker = _worker or PipelineWorker.from_env()
    ran = worker.process_one()
    return {"processed": ran}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("CLASSIFICATION_SERVER_PORT", "8020"))
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    uvicorn.run(app, host=os.environ.get("CLASSIFICATION_SERVER_HOST", "127.0.0.1"), port=port)


if __name__ == "__main__":
    main()
