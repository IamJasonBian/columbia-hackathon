from __future__ import annotations

import json
from pathlib import Path

from extraction_service.models import ExtractedDocument
from extraction_service.quote.poller_ingest import persist_extracted_document


def test_ingest_from_pull_json(tmp_path: Path, monkeypatch):
    pulls = Path(__file__).resolve().parents[1] / "app_storage/pulls/jason.bian57@gmail.com"
    sample = pulls / "1a126fd5d1c0d6fb.json"
    if not sample.exists():
        return

    saved: list[tuple] = []

    class FakeRepo:
        def save_quote_bundle(self, header, items, compliance):
            saved.append((header.gmail_message_id, len(items), len(compliance)))
            return 99

        def get_line_items(self, quote_id):
            return []

    events: list[str] = []

    class FakeQueue:
        def enqueue(self, *a, **k):
            events.append("enqueued")
            return type("J", (), {"id": 1})()

        def set_stage(self, job_id, stage):
            events.append(stage)

        def set_postgres_quote_id(self, job_id, quote_id):
            events.append("linked")

        def complete(self, job_id):
            events.append("completed")

        def fail(self, job_id, error):
            events.append("failed")

    doc = ExtractedDocument.model_validate(json.loads(sample.read_text()))
    qid = persist_extracted_document(
        "1a126fd5d1c0d6fb",
        doc,
        mailbox="jason.bian57@gmail.com",
        repo=FakeRepo(),  # type: ignore[arg-type]
        queue=FakeQueue(),  # type: ignore[arg-type]
        job_id=1,
    )
    assert qid == 99
    assert saved[0][1] > 0
    assert "persist" in events
    assert "completed" in events
    assert "enqueued" not in events
