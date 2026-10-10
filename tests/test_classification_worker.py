from __future__ import annotations

import json
from pathlib import Path

from services.extraction.db.sqlite_queue import ServiceQueue
from services.extraction.models import DocumentKind, ExtractedDocument
from services.extraction.nicrron import (
    ComplianceDocumentAssessment,
    NicrronEmailComplianceAssessment,
)
from services.extraction.quote.classification_worker import (
    ClassificationWorker,
    enqueue_classification,
)
from services.extraction.quote.customer_compliance import load_customer_compliance


class FakeRepository:
    def __init__(self):
        self.classifications: list[dict] = []
        self.compliance: list[tuple[int, str, list]] = []

    def get_lines(self, quote_id):
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


def test_worker_fans_customer_requirements_out_to_every_line(
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
        "services.extraction.quote.classification_worker.assess_email_compliance",
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
    assert {(record.line_id, record.requirement_code) for record in records} == {
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
