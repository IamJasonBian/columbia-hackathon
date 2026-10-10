"""Local sender-to-customer compliance requirements."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path

from pydantic import BaseModel, Field


class ComplianceDocumentRequirement(BaseModel):
    code: str = Field(min_length=1)
    document_name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


class CustomerCompliance(BaseModel):
    customer_id: str = Field(min_length=1)
    sender_emails: list[str] = Field(default_factory=list)
    sender_domains: list[str] = Field(default_factory=list)
    compliance_documents: list[ComplianceDocumentRequirement] = Field(default_factory=list)


@dataclass
class CustomerComplianceMapping:
    customers: list[CustomerCompliance]
    _emails: dict[str, CustomerCompliance] = field(init=False, default_factory=dict)
    _domains: dict[str, CustomerCompliance] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        customer_ids: set[str] = set()
        for customer in self.customers:
            customer_id = customer.customer_id.strip()
            if customer_id in customer_ids:
                raise ValueError(f"duplicate customer_id {customer_id!r}")
            customer_ids.add(customer_id)

            requirement_codes: set[str] = set()
            for requirement in customer.compliance_documents:
                code = requirement.code.strip().lower()
                if code in requirement_codes:
                    raise ValueError(
                        f"duplicate requirement code {requirement.code!r} for {customer_id!r}"
                    )
                requirement_codes.add(code)
                requirement.code = code

            for raw_email in customer.sender_emails:
                email = parseaddr(raw_email)[1].strip().lower()
                if not email or "@" not in email:
                    raise ValueError(f"invalid sender email {raw_email!r}")
                if email in self._emails:
                    raise ValueError(f"duplicate sender email {email!r}")
                self._emails[email] = customer

            for raw_domain in customer.sender_domains:
                domain = raw_domain.strip().lower().lstrip("@")
                if not domain or "." not in domain:
                    raise ValueError(f"invalid sender domain {raw_domain!r}")
                if domain in self._domains:
                    raise ValueError(f"duplicate sender domain {domain!r}")
                self._domains[domain] = customer

    def resolve(self, sender: str | None) -> CustomerCompliance | None:
        """Resolve an RFC 822 sender, preferring an exact address over its domain."""
        email = parseaddr(sender or "")[1].strip().lower()
        if not email or "@" not in email:
            return None
        exact = self._emails.get(email)
        if exact is not None:
            return exact
        return self._domains.get(email.rsplit("@", 1)[1])


def default_mapping_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return Path(os.environ.get("CUSTOMER_COMPLIANCE_PATH", root / "config/customer_compliance.json"))


def load_customer_compliance(path: Path | None = None) -> CustomerComplianceMapping:
    mapping_path = path or default_mapping_path()
    raw = json.loads(mapping_path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("customers"), list):
        raise ValueError("customer compliance mapping must contain a customers array")
    customers = [CustomerCompliance.model_validate(item) for item in raw["customers"]]
    return CustomerComplianceMapping(customers)


__all__ = [
    "ComplianceDocumentRequirement",
    "CustomerCompliance",
    "CustomerComplianceMapping",
    "default_mapping_path",
    "load_customer_compliance",
]
