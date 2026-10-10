"""Email classification and compliance assessment through Nicrron's OpenAI-compatible API."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from services.extraction.models import DocumentKind, ExtractedDocument
from services.extraction.prism import emit_trace
from services.extraction.quote.customer_compliance import ComplianceDocumentRequirement

DEFAULT_MODEL = "nicrron/fastest"
DEFAULT_BASE_URL = "https://api.nicrron.ai/v1"

# Keep the prompt well under the context window; long spreadsheets get cut.
MAX_INPUT_CHARS = 200_000


class NicrronExtractionError(RuntimeError):
    """The Nicrron request failed or returned unusable JSON."""


class ComplianceDocumentAssessment(BaseModel):
    requirement_code: str
    status: Literal["satisfied", "document_missing"]
    attachment_path: str | None = None
    confidence: float = Field(ge=0, le=1)
    rationale: str

    @model_validator(mode="after")
    def validate_evidence(self) -> ComplianceDocumentAssessment:
        if self.status == "satisfied" and not self.attachment_path:
            raise ValueError("satisfied compliance documents require attachment_path")
        if self.status == "document_missing":
            self.attachment_path = None
        return self


class NicrronEmailComplianceAssessment(BaseModel):
    model: str
    email_label: Literal["rfq", "invoice", "purchase_order", "remittance", "other"]
    email_confidence: float = Field(ge=0, le=1)
    compliance_documents: list[ComplianceDocumentAssessment] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False


def render_document_for_prompt(doc: ExtractedDocument, _depth: int = 0) -> str:
    """Flatten a document (and its attachments) into prompt text.

    Args:
        doc: The extracted document.

    Returns:
        A labelled plain-text rendering: metadata, body/page text, tables.
    """
    indent = "  " * _depth
    parts = [f"{indent}=== {doc.kind.value.upper()}: {doc.filename} ==="]
    meta = {k: v for k, v in doc.metadata.items() if v not in (None, [], "")}
    if meta:
        parts.append(f"{indent}metadata: {json.dumps(meta, default=str)}")
    if doc.kind == DocumentKind.spreadsheet:
        # Tables carry everything for spreadsheets; skip the duplicate text.
        for table in doc.tables:
            parts.append(f"{indent}--- sheet: {table.name} ---")
            parts.append("\t".join(table.headers))
            parts.extend("\t".join("" if v is None else str(v) for v in row) for row in table.rows)
    else:
        for page in doc.pages or []:
            parts.append(f"{indent}--- page {page.index + 1}{' (OCR)' if page.ocr else ''} ---")
            parts.append(page.text)
        if not doc.pages and doc.text:
            parts.append(doc.text)
        for table in doc.tables:
            parts.append(f"{indent}--- table: {table.name} ---")
            parts.append("\t".join(table.headers))
            parts.extend("\t".join("" if v is None else str(v) for v in row) for row in table.rows)
    for attachment in doc.attachments:
        parts.append(render_document_for_prompt(attachment, _depth + 1))
    return "\n".join(parts)


def _client() -> Any:
    from openai import OpenAI

    api_key = os.environ.get("NICRRON_API_KEY")
    if not api_key:
        raise NicrronExtractionError("Set NICRRON_API_KEY to use Nicrron extraction")
    return OpenAI(
        api_key=api_key,
        base_url=os.environ.get("NICRRON_BASE_URL", DEFAULT_BASE_URL),
    )


def _json_completion(
    doc: ExtractedDocument,
    *,
    system: str,
    model: str | None,
    client: Any,
    agent_id: str,
    agent_name: str,
) -> tuple[str, dict[str, Any], bool, dict[str, Any]]:
    selected_model = model or os.environ.get("NICRRON_MODEL", DEFAULT_MODEL)
    text = render_document_for_prompt(doc)
    truncated = len(text) > MAX_INPUT_CHARS
    text = text[:MAX_INPUT_CHARS]

    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=selected_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        )
        raw = response.choices[0].message.content
    except Exception as exc:
        raise NicrronExtractionError(f"Nicrron request failed: {exc}") from exc

    latency_ms = round((time.perf_counter() - started) * 1000)
    if not isinstance(raw, str):
        raise NicrronExtractionError("Nicrron returned an empty response")
    normalized = raw.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1]).strip()
    usage = response.usage.model_dump() if getattr(response, "usage", None) else {}
    trace_args = {
        "model": selected_model,
        "input_messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "output_message": raw,
        "latency_ms": latency_ms,
        "usage": usage,
        "session_id": str(doc.metadata.get("message_id") or doc.filename),
        "agent_id": agent_id,
        "agent_name": agent_name,
    }
    try:
        data = json.loads(normalized)
    except json.JSONDecodeError as exc:
        emit_trace(**trace_args, metadata={"provider": "nicrron", "status": "invalid_json"})
        raise NicrronExtractionError(f"Nicrron returned non-JSON output: {raw[:200]!r}") from exc
    if not isinstance(data, dict):
        emit_trace(**trace_args, metadata={"provider": "nicrron", "status": "invalid_shape"})
        raise NicrronExtractionError("Nicrron response must be a JSON object")

    emit_trace(**trace_args, metadata={"provider": "nicrron", "status": "success"})
    return selected_model, usage, truncated, data


def assess_email_compliance(
    doc: ExtractedDocument,
    requirements: list[ComplianceDocumentRequirement],
    *,
    instructions: str | None = None,
    model: str | None = None,
    client: Any = None,
) -> NicrronEmailComplianceAssessment:
    """Classify an email and match parsed attachments to configured requirements."""
    requirement_payload = [
        {
            "requirement_code": requirement.code,
            "document_name": requirement.document_name,
            "matching_prompt": requirement.prompt,
        }
        for requirement in requirements
    ]
    system = (
        "Classify the supplied email as exactly one of: rfq, invoice, purchase_order, "
        "remittance, other. Then evaluate every configured compliance requirement against "
        "the parsed attachments. Use attachment filename and parsed contents as evidence; "
        "do not infer that a document exists when it is absent. Return one JSON object with "
        'keys "email_label", "email_confidence", and "compliance_documents". Each compliance '
        'entry must contain "requirement_code", "status" (satisfied or document_missing), '
        '"attachment_path", "confidence", and "rationale". Return exactly one entry for each '
        "configured requirement.\n\nConfigured requirements:\n"
        + json.dumps(requirement_payload, indent=2)
    )
    if instructions:
        system += f"\n\nAdditional guidance:\n{instructions}"

    actual_client = client or _client()
    selected_model, usage, truncated, payload = _json_completion(
        doc,
        system=system,
        model=model,
        client=actual_client,
        agent_id="nicrron-compliance",
        agent_name="Nicrron Compliance Assessment",
    )
    try:
        result = NicrronEmailComplianceAssessment(
            model=selected_model,
            usage=usage,
            truncated=truncated,
            **payload,
        )
    except (TypeError, ValidationError) as exc:
        raise NicrronExtractionError(f"invalid email compliance response: {exc}") from exc

    expected_codes = {requirement.code for requirement in requirements}
    returned_codes = [item.requirement_code for item in result.compliance_documents]
    if len(returned_codes) != len(set(returned_codes)) or set(returned_codes) != expected_codes:
        raise NicrronExtractionError(
            "Nicrron compliance requirement codes did not exactly match the configured "
            f"requirement codes; expected {sorted(expected_codes)!r}, got {sorted(returned_codes)!r}"
        )
    return result
