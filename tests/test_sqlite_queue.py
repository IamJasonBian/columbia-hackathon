from __future__ import annotations

from pathlib import Path

from services.extraction.db.sqlite_queue import ServiceQueue


def test_enqueue_claim_complete(tmp_path: Path):
    q = ServiceQueue(tmp_path / "q.db")
    job = q.enqueue("rfq_quote", idempotency_key="a:1", mailbox="a", gmail_message_id="1")
    assert job.status == "pending"

    dup = q.enqueue("rfq_quote", idempotency_key="a:1", mailbox="a", gmail_message_id="1")
    assert dup.id == job.id

    claimed = q.claim_next()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.attempts == 1

    q.set_postgres_quote_id(job.id, 42)
    q.complete(job.id)
    done = q.get(job.id)
    assert done is not None
    assert done.status == "succeeded"
    assert done.postgres_quote_id == 42


def test_fail_retries_then_dead(tmp_path: Path):
    q = ServiceQueue(tmp_path / "q.db")
    job = q.enqueue("rfq_quote", max_attempts=2)
    q.claim_next()
    q.fail(job.id, "boom", retry_delay_seconds=0)
    pending = q.get(job.id)
    assert pending is not None
    assert pending.status == "pending"

    q.claim_next()
    q.fail(job.id, "boom again", retry_delay_seconds=0)
    dead = q.get(job.id)
    assert dead is not None
    assert dead.status == "dead"
