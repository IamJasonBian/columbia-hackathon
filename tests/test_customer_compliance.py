from __future__ import annotations

import json

import pytest

from extraction_service.quote.customer_compliance import load_customer_compliance


def _write_mapping(tmp_path, customers):
    path = tmp_path / "customer_compliance.json"
    path.write_text(json.dumps({"customers": customers}))
    return path


def test_resolves_exact_sender_before_domain(tmp_path):
    path = _write_mapping(
        tmp_path,
        [
            {
                "customer_id": "domain-customer",
                "sender_domains": ["buyer.example"],
                "compliance_documents": [
                    {
                        "code": "ein",
                        "document_name": "EIN Letter",
                        "prompt": "Find an IRS EIN assignment letter.",
                    }
                ],
            },
            {
                "customer_id": "exact-customer",
                "sender_emails": ["vip@buyer.example"],
                "compliance_documents": [
                    {
                        "code": "doc",
                        "document_name": "Declaration of Compliance",
                        "prompt": "Find a signed declaration.",
                    }
                ],
            },
        ],
    )

    mapping = load_customer_compliance(path)

    assert mapping.resolve("VIP Buyer <vip@buyer.example>").customer_id == "exact-customer"
    assert mapping.resolve("other@buyer.example").customer_id == "domain-customer"
    assert mapping.resolve("unknown@example.net") is None


def test_rejects_duplicate_sender_and_requirement_codes(tmp_path):
    duplicate_sender = _write_mapping(
        tmp_path,
        [
            {"customer_id": "a", "sender_domains": ["example.com"], "compliance_documents": []},
            {"customer_id": "b", "sender_domains": ["EXAMPLE.COM"], "compliance_documents": []},
        ],
    )
    with pytest.raises(ValueError, match="sender domain"):
        load_customer_compliance(duplicate_sender)

    duplicate_code = _write_mapping(
        tmp_path,
        [
            {
                "customer_id": "a",
                "sender_emails": ["a@example.com"],
                "compliance_documents": [
                    {"code": "ein", "document_name": "one", "prompt": "one"},
                    {"code": "EIN", "document_name": "two", "prompt": "two"},
                ],
            }
        ],
    )
    with pytest.raises(ValueError, match="requirement code"):
        load_customer_compliance(duplicate_code)


def test_rejects_duplicate_customer_ids(tmp_path):
    path = _write_mapping(
        tmp_path,
        [
            {"customer_id": "same", "sender_emails": ["a@example.com"], "compliance_documents": []},
            {"customer_id": "same", "sender_emails": ["b@example.com"], "compliance_documents": []},
        ],
    )
    with pytest.raises(ValueError, match="customer_id"):
        load_customer_compliance(path)
