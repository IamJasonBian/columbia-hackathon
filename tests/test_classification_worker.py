from __future__ import annotations

import json
import logging
from pathlib import Path

from extraction_service.db.sqlite_queue import ServiceQueue
from extraction_service.models import DocumentKind, ExtractedDocument
from extraction_service.nicrron import (
    ComplianceDocumentAssessment,
    NicrronEmailComplianceAssessment,
    NicrronExtractionError,
)
from extraction_service.quote.classification_worker import (
    ClassificationWorker,
    enqueue_classification,
)
from extraction_service.quote.customer_compliance import load_customer_compliance


class FakeRepository:
    def __init__(self):
        self.classifications: list[dict] = []
        self.compliance: list[tuple[int, str, list]] = []

    def get_line_items(self, quote_id):
        return [{"id": 11, "line_seq": 1}, {"id": 12, "line_seq": 2}]

    def update_classification(self, quote_id, **kwargs):
        self.classifications.append({"quote_id": quote_id, **kwargs})

    def replace_compliance_source(self, quote_id, source, records):
        self.compliance.append((quote_id, source, records))


def _write_document(root: Path, sender: str) -> None:
    doc = ExtractedDocument(
        filename="message.eml",
        kind=DocumentKind.email,
        text="Please quote both items.",
        metadata={"from": sender, "subject": "RFQ"},
        attachments=[
            ExtractedDocument(
                filename="ein.pdf",
                kind=DocumentKind.pdf,
                text="IRS Employer Identification Number 12-3456789",
            )
        ],
    )
    target = root / "ops@example.com"
    target.mkdir(parents=True)
    (target / "mid.json").write_text(json.dumps(doc.model_dump(mode="json")))


def _mapping(tmp_path: Path):
    path = tmp_path / "mapping.json"
    path.write_text(
        json.dumps(
            {
                "customers": [
                    {
                        "customer_id": "customer-1",
                        "sender_domains": ["buyer.example"],
                        "compliance_documents": [
                            {"code": "ein", "document_name": "EIN", "prompt": "Find EIN"},
                            {"code": "doc", "document_name": "DoC", "prompt": "Find DoC"},
                        ],
                    }
                ]
            }
        )
    )
    return load_customer_compliance(path)


def test_worker_fans_customer_requirements_out_to_every_line_item(
    tmp_path: Path, monkeypatch
):
    pulls = tmp_path / "pulls"
    _write_document(pulls, "Buyer <rfq@buyer.example>")
    queue = ServiceQueue(tmp_path / "queue.db")
    job_id = enqueue_classification(
        queue,
        mailbox="ops@example.com",
        gmail_message_id="mid",
        quote_id=99,
    )
    repo = FakeRepository()

    def fake_assess(doc, requirements, **kwargs):
        assert [item.code for item in requirements] == ["ein", "doc"]
        return NicrronEmailComplianceAssessment(
            model="nicrron/fastest",
            email_label="rfq",
            email_confidence=0.98,
            compliance_documents=[
                ComplianceDocumentAssessment(
                    requirement_code="ein",
                    status="satisfied",
                    attachment_path="ein.pdf",
                    confidence=0.95,
                    rationale="Matched parsed EIN letter",
                ),
                ComplianceDocumentAssessment(
                    requirement_code="doc",
                    status="document_missing",
                    confidence=0.9,
                    rationale="No declaration attached",
                ),
            ],
        )

    monkeypatch.setenv("NICRRON_API_KEY", "test-key")
    monkeypatch.setattr(
        "extraction_service.quote.classification_worker.assess_email_compliance",
        fake_assess,
    )
    worker = ClassificationWorker(
        queue=queue,
        sink=None,
        pulls_root=pulls,
        customer_mapping=_mapping(tmp_path),
        repository=repo,  # type: ignore[arg-type]
    )

    assert worker.process_one()

    assert repo.classifications[0]["customer_id"] == "customer-1"
    assert repo.classifications[0]["human_review"] is False
    _, source, records = repo.compliance[0]
    assert source == "customer_mapping"
    assert len(records) == 4
    assert {(record.line_item_id, record.requirement_code) for record in records} == {
        (11, "ein"),
        (11, "doc"),
        (12, "ein"),
        (12, "doc"),
    }
    ein = next(record for record in records if record.requirement_code == "ein")
    assert ein.status == "satisfied"
    assert ein.attachment_path == "ein.pdf"
    saved = queue.get(job_id)
    assert saved is not None and saved.status == "succeeded"
    assert saved.payload["classification"]["email_label"] == "rfq"


def test_worker_routes_unmapped_sender_to_human_review(tmp_path: Path, monkeypatch):
    pulls = tmp_path / "pulls"
    _write_document(pulls, "unknown@elsewhere.example")
    queue = ServiceQueue(tmp_path / "queue.db")
    enqueue_classification(
        queue,
        mailbox="ops@example.com",
        gmail_message_id="mid",
        quote_id=99,
    )
    repo = FakeRepository()

    def fake_assess(doc, requirements, **kwargs):
        assert requirements == []
        return NicrronEmailComplianceAssessment(
            model="nicrron/fastest",
            email_label="rfq",
            email_confidence=0.8,
            compliance_documents=[],
        )

    monkeypatch.setenv("NICRRON_API_KEY", "test-key")
    monkeypatch.setattr(
        "extraction_service.quote.classification_worker.assess_email_compliance",
        fake_assess,
    )
    worker = ClassificationWorker(
        queue=queue,
        sink=None,
        pulls_root=pulls,
        customer_mapping=_mapping(tmp_path),
        repository=repo,  # type: ignore[arg-type]
    )

    assert worker.process_one()

    assert repo.classifications[0]["customer_id"] is None
    assert repo.classifications[0]["human_review"] is True
    assert repo.compliance == [(99, "customer_mapping", [])]


def test_worker_returns_null_and_logs_when_nicrron_cannot_classify(
    tmp_path: Path, monkeypatch, caplog
):
    pulls = tmp_path / "pulls"
    _write_document(pulls, "rfq@buyer.example")
    queue = ServiceQueue(tmp_path / "queue.db")
    job_id = enqueue_classification(
        queue,
        mailbox="ops@example.com",
        gmail_message_id="mid",
        quote_id=99,
    )
    repo = FakeRepository()

    def fail_assessment(*args, **kwargs):
        raise NicrronExtractionError("invalid model output")

    monkeypatch.setenv("NICRRON_API_KEY", "test-key")
    monkeypatch.setattr(
        "extraction_service.quote.classification_worker.assess_email_compliance",
        fail_assessment,
    )
    worker = ClassificationWorker(
        queue=queue,
        sink=None,
        pulls_root=pulls,
        customer_mapping=_mapping(tmp_path),
        repository=repo,  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING):
        assert worker.process_one()

    saved = queue.get(job_id)
    assert saved is not None and saved.status == "succeeded"
    assert saved.payload["classification"] is None
    assert "classification unavailable" in caplog.text
    assert repo.classifications == []
    assert repo.compliance == []
