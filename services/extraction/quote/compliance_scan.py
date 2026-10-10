"""Regex-based compliance detection from RFQ text (generic requirement codes)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.extraction.quote.models import ComplianceRecord


@dataclass(frozen=True)
class CompliancePattern:
    requirement_code: str
    requirement_label: str
    required_document_type: str | None
    pattern: re.Pattern[str]


DEFAULT_COMPLIANCE_PATTERNS: tuple[CompliancePattern, ...] = (
    CompliancePattern(
        "mtr",
        "Mill test report (MTR/MTC)",
        "mill_test_report",
        re.compile(r"(?i)\b(mill\s+test\s+(report|certificate)|\bMTR\b|\bMTC\b)\b"),
    ),
    CompliancePattern(
        "coo",
        "Country of origin",
        "country_of_origin",
        re.compile(r"(?i)\b(country\s+of\s+origin|\bCOO\b|origin\s+certificate)\b"),
    ),
    CompliancePattern(
        "spec_cert",
        "Material / spec certification",
        "spec_certification",
        re.compile(
            r"(?i)\b("
            r"ASTM\s*[A-Z]?\d+\w*|"
            r"material\s+cert(ification)?|"
            r"spec(ification)?\s+cert(ification)?|"
            r"3\.1\s+cert(ificate)?"
            r")\b"
        ),
    ),
    CompliancePattern(
        "attachment_required",
        "Supporting documentation required",
        "supporting_document",
        re.compile(
            r"(?i)\b("
            r"attach(ed)?\s+(copy|documentation|certificate)|"
            r"supporting\s+documentation|"
            r"must\s+include\s+(the\s+)?(certificate|documentation)"
            r")\b"
        ),
    ),
)


def scan_compliance_text(
    text: str,
    *,
    patterns: tuple[CompliancePattern, ...] = DEFAULT_COMPLIANCE_PATTERNS,
) -> list[ComplianceRecord]:
    """Return one ComplianceRecord per pattern that matches anywhere in ``text``."""
    if not text.strip():
        return []
    found: list[ComplianceRecord] = []
    for rule in patterns:
        match = rule.pattern.search(text)
        if not match:
            continue
        found.append(
            ComplianceRecord(
                requirement_code=rule.requirement_code,
                requirement_label=rule.requirement_label,
                matched_pattern=rule.pattern.pattern,
                source_text=match.group(0),
                required_document_type=rule.required_document_type,
                status="required",
            )
        )
    return found
