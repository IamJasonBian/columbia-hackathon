from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.extraction.extractors import extract_document
from services.extraction.nicrron import NicrronExtractionError, assess_email_compliance
from services.extraction.models import DocumentKind, ExtractedDocument
from services.extraction.quote.customer_compliance import ComplianceDocumentRequirement


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
