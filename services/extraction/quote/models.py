"""Normalized RFQ quote rows (Postgres) and local job payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

LineStatus = Literal[
    "pending",
    "parsed",
    "matched",
    "priced",
    "ambiguous",
    "unmatched",
    "rejected",
]

RfqLineFormat = Literal["numbered", "spec_ref", "description_part"]

ComplianceStatus = Literal[
    "required",
    "document_missing",
    "satisfied",
    "waived",
    "send_to_buyer",
]

JobStatus = Literal["pending", "running", "succeeded", "failed", "dead"]

QuoteStage = Literal[
    "intake",
    "parse_lines",
    "match_catalog",
    "price",
    "compliance",
    "finalize",
]


class LineRecord(BaseModel):
    line_seq: int
    line_no: int | None = None
    rfq_format: RfqLineFormat = "numbered"
    buyer_part_no: str | None = None
    description: str
    quantity: float | None = None
    unit_of_measure: str | None = None
    unit_price: float | None = None
    delivery: str | None = None
    is_option: bool = False
    required_qty_raw: str | None = None
    source_attachment: str | None = None
    source_page: int | None = None
    source_table: str | None = None
    raw_row: dict[str, Any] = Field(default_factory=dict)
    match_score: float | None = None
    quoted_unit_price: float | None = None
    extended_price: float | None = None
    market_benchmark_price: float | None = None
    price_rationale: str | None = None
    line_status: LineStatus = "parsed"
    clarifying_question: str | None = None
    availability_note: str | None = None

    @classmethod
    def from_extracted(cls, row: dict[str, Any]) -> LineRecord:
        return cls(
            line_seq=int(row["line_seq"]),
            line_no=row.get("line_no"),
            rfq_format=row.get("rfq_format") or "numbered",
            buyer_part_no=row.get("buyer_part_no"),
            description=row["description"],
            quantity=row.get("quantity"),
            unit_of_measure=row.get("unit_of_measure"),
            unit_price=row.get("unit_price"),
            delivery=row.get("delivery"),
            is_option=bool(row.get("is_option")),
            required_qty_raw=row.get("required_qty_raw"),
            source_attachment=row.get("source_attachment"),
            source_page=row.get("source_page"),
            source_table=row.get("source_table"),
            raw_row=row.get("raw_row") or {},
            line_status="parsed",
        )


class ComplianceRecord(BaseModel):
    """Generic compliance flag (MTR, COO, certs, attachments, custom codes)."""

    requirement_code: str
    requirement_label: str
    source: str = "email_text"
    line_id: int | None = None
    matched_pattern: str | None = None
    source_text: str | None = None
    status: ComplianceStatus = "required"
    required_document_type: str | None = None
    attachment_path: str | None = None
    buyer_message: str | None = None


class RfqQuoteHeader(BaseModel):
    mailbox: str | None = None
    gmail_message_id: str | None = None
    customer_id: str | None = None
    rfq_number: str | None = None
    email_subject: str | None = None
    source_message_date: str | None = None
    buyer_organization: str | None = None
    workflow_status: str = "intake"
    reject_reason: str | None = None
    has_attachments: bool = False
    parsed: dict[str, Any] | None = None
