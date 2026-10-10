from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SKIP_AUTH", "1")

from extraction_service.extractors import extract_document
import extraction_service.main as main_module
from extraction_service.nicrron import (
    NicrronClassification,
    NicrronExtractionError,
    assess_email_compliance,
    classify_document,
    extract_with_nicrron,
)
from extraction_service.models import DocumentKind, ExtractedDocument
from extraction_service.quote.customer_compliance import ComplianceDocumentRequirement


class FakeCompletions:
    def __init__(self, payload: dict, *, fenced: bool = False):
        self.payload = payload
        self.fenced = fenced
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(self.payload)
        if self.fenced:
            content = f"```json\n{content}\n```"
        message = SimpleNamespace(content=content)
        usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 12, "completion_tokens": 8})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def fake_client(payload: dict, *, fenced: bool = False) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(payload, fenced=fenced)))


def test_classify_document_uses_allowed_labels_and_deterministic_json():
    client = fake_client(
        {"label": "rfq", "confidence": 0.96, "rationale": "Requests pricing for steel"}
    )
    doc = extract_document(b"Please quote 40 sheets of 304 stainless.", "rfq.txt")

    result = classify_document(doc, ["rfq", "invoice", "other"], client=client)

    assert result.label == "rfq"
    assert result.confidence == 0.96
    assert result.usage["prompt_tokens"] == 12
    call = client.chat.completions.calls[0]
    assert call["model"] == "nicrron/fastest"
    assert call["temperature"] == 0
    assert call["response_format"] == {"type": "json_object"}
    assert '"rfq", "invoice", "other"' in call["messages"][0]["content"]
    assert "Please quote 40 sheets" in call["messages"][1]["content"]


def test_classify_document_rejects_label_outside_allowlist():
    client = fake_client({"label": "purchase_order", "confidence": 0.8, "rationale": "PO"})
    doc = extract_document(b"PO 123", "po.txt")

    with pytest.raises(NicrronExtractionError, match="not one of"):
        classify_document(doc, ["rfq", "invoice"], client=client)


def test_classify_document_accepts_markdown_fenced_json():
    client = fake_client(
        {"label": "rfq", "confidence": 0.95, "rationale": "Requests a quote"},
        fenced=True,
    )
    doc = extract_document(b"Please quote this order", "rfq.txt")

    result = classify_document(doc, ["rfq", "other"], client=client)

    assert result.label == "rfq"


def test_extract_with_nicrron_returns_structured_json():
    client = fake_client({"rfq_number": "RFQ-42", "line_items": [{"quantity": 40}]})
    doc = extract_document(b"RFQ-42: quote 40 sheets", "rfq.txt")

    result = extract_with_nicrron(
        doc,
        instructions="Extract the RFQ number and line items.",
        client=client,
        model="nicrron/auto",
    )

    assert result.data["rfq_number"] == "RFQ-42"
    assert result.model == "nicrron/auto"
    assert "Extract the RFQ number" in client.chat.completions.calls[0]["messages"][0]["content"]


def test_assess_email_compliance_matches_parsed_attachment():
    client = fake_client(
        {
            "email_label": "rfq",
            "email_confidence": 0.98,
            "compliance_documents": [
                {
                    "requirement_code": "ein",
                    "status": "satisfied",
                    "attachment_path": "EIN-letter.pdf",
                    "confidence": 0.93,
                    "rationale": "The parsed IRS letter states an EIN.",
                },
                {
                    "requirement_code": "food_safety_plan_haccp",
                    "status": "document_missing",
                    "attachment_path": None,
                    "confidence": 0.9,
                    "rationale": "No HACCP plan is attached.",
                },
            ],
        }
    )
    doc = ExtractedDocument(
        filename="message.eml",
        kind=DocumentKind.email,
        text="Please quote the attached items.",
        attachments=[
            ExtractedDocument(
                filename="EIN-letter.pdf",
                kind=DocumentKind.pdf,
                text="Internal Revenue Service Employer Identification Number 12-3456789",
            )
        ],
    )
    requirements = [
        ComplianceDocumentRequirement(
            code="ein",
            document_name="Mock EIN Letter.pdf",
            prompt="Find an official IRS EIN assignment letter.",
        ),
        ComplianceDocumentRequirement(
            code="food_safety_plan_haccp",
            document_name="Mock Food Safety Plan HACCP.pdf",
            prompt="Find a HACCP food safety plan.",
        ),
    ]

    result = assess_email_compliance(doc, requirements, client=client)

    assert result.email_label == "rfq"
    assert result.compliance_documents[0].attachment_path == "EIN-letter.pdf"
    call = client.chat.completions.calls[0]
    assert "Mock Food Safety Plan HACCP.pdf" in call["messages"][0]["content"]
    assert "Find a HACCP food safety plan." in call["messages"][0]["content"]
    assert "EIN-letter.pdf" in call["messages"][1]["content"]
    assert "12-3456789" in call["messages"][1]["content"]


def test_assess_email_compliance_rejects_missing_or_unknown_requirement_codes():
    requirements = [
        ComplianceDocumentRequirement(code="ein", document_name="EIN", prompt="Find EIN"),
        ComplianceDocumentRequirement(code="doc", document_name="DoC", prompt="Find DoC"),
    ]
    doc = extract_document(b"hello", "message.txt")
    client = fake_client(
        {
            "email_label": "other",
            "email_confidence": 0.6,
            "compliance_documents": [
                {
                    "requirement_code": "unknown",
                    "status": "document_missing",
                    "attachment_path": None,
                    "confidence": 0.5,
                    "rationale": "Missing",
                }
            ],
        }
    )

    with pytest.raises(NicrronExtractionError, match="requirement codes"):
        assess_email_compliance(doc, requirements, client=client)


def test_classify_document_requires_labels():
    doc = extract_document(b"hello", "note.txt")
    with pytest.raises(ValueError, match="label"):
        classify_document(doc, [])


def test_nicrron_classification_endpoint_extracts_and_saves(monkeypatch):
    def fake_classify(doc, labels, **kwargs):
        assert doc.text == "Please quote this order"
        assert labels == ["rfq", "invoice", "other"]
        return NicrronClassification(
            model="nicrron/fastest",
            label="rfq",
            confidence=0.98,
            rationale="Requests a quote",
        )

    monkeypatch.setattr(main_module, "classify_document", fake_classify, raising=False)
    response = TestClient(main_module.app).post(
        "/extract/nicrron",
        files={"file": ("request.txt", b"Please quote this order", "text/plain")},
        data={"labels": '["rfq", "invoice", "other"]'},
    )

    assert response.status_code == 200
    row = response.json()
    assert row["document"]["kind"] == "text"
    assert row["structured"]["provider"] == "nicrron"
    assert row["structured"]["classification"]["label"] == "rfq"


def test_nicrron_classification_endpoint_rejects_bad_labels():
    response = TestClient(main_module.app).post(
        "/extract/nicrron",
        files={"file": ("request.txt", b"hello", "text/plain")},
        data={"labels": "rfq,invoice"},
    )
    assert response.status_code == 400
    assert "JSON array" in response.json()["detail"]


def test_nicrron_endpoint_returns_null_and_logs_classification_failure(
    monkeypatch, caplog
):
    def fail_classification(*args, **kwargs):
        raise NicrronExtractionError("invalid model output")

    monkeypatch.setattr(main_module, "classify_document", fail_classification)
    with caplog.at_level(logging.WARNING):
        response = TestClient(main_module.app).post(
            "/extract/nicrron",
            files={"file": ("request.txt", b"Please quote this order", "text/plain")},
            data={"labels": '["rfq", "other"]'},
        )

    assert response.status_code == 200
    assert response.json()["structured"]["classification"] is None
    assert "classification unavailable" in caplog.text
