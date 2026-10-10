from __future__ import annotations

import json


from services.extraction.quote.customer_compliance import load_customer_compliance


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
