"""OpenAI structured extraction over an ExtractedDocument.

The format extractors turn email/PDF/spreadsheet/image bytes into text and
tables; this module hands that to an OpenAI model (Responses API) and asks for
JSON back — either against a caller-supplied JSON Schema (structured outputs)
or free-form JSON guided by instructions. With ``vision=True`` the page images
of scanned PDFs/images are sent too, so the model can read what OCR missed.
"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

from pydantic import BaseModel, Field

from extraction_service.extractors.image_extractor import load_image_pages
from extraction_service.extractors.pdf_extractor import render_pdf_pages
from extraction_service.models import DocumentKind, ExtractedDocument

DEFAULT_MODEL = "gpt-5-mini"

# Keep the prompt well under the context window; long spreadsheets get cut.
MAX_INPUT_CHARS = 200_000
MAX_VISION_PAGES = 10

DEFAULT_INSTRUCTIONS = (
    "You extract structured data from business documents (emails, PDFs, "
    "spreadsheets, scanned forms). Return only facts present in the document. "
    "Use null for anything missing; never invent values. Keep numbers as "
    "numbers and dates as ISO 8601 (YYYY-MM-DD)."
)


class LlmExtractionError(RuntimeError):
    """The OpenAI call failed or returned unparseable output."""


class LlmExtraction(BaseModel):
    model: str
    data: Any = Field(description="Parsed JSON returned by the model")
    usage: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = Field(False, description="Input text was cut at MAX_INPUT_CHARS")


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


def _image_inputs(doc: ExtractedDocument, data: bytes) -> list[dict[str, Any]]:
    if doc.kind == DocumentKind.pdf:
        pages = render_pdf_pages(data, resolution=150)
    elif doc.kind == DocumentKind.image:
        pages = load_image_pages(data)
    else:
        return []
    inputs: list[dict[str, Any]] = []
    for page in pages[:MAX_VISION_PAGES]:
        buffer = io.BytesIO()
        page.convert("RGB").save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        inputs.append({"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"})
    return inputs


def _client():  # noqa: ANN202 — keeps the openai import lazy
    from openai import OpenAI

    if not os.environ.get("OPENAI_API_KEY"):
        raise LlmExtractionError("Set OPENAI_API_KEY to use structured extraction")
    return OpenAI()


def extract_structured(
    doc: ExtractedDocument,
    *,
    instructions: str | None = None,
    schema: dict[str, Any] | None = None,
    schema_name: str = "extraction",
    strict: bool = False,
    model: str | None = None,
    vision: bool = False,
    source_bytes: bytes | None = None,
    client: Any = None,
) -> LlmExtraction:
    """Ask an OpenAI model to turn an extracted document into JSON.

    Args:
        doc: Output of extraction_service.extractors.extract_document.
        instructions: What to pull out (appended to DEFAULT_INSTRUCTIONS).
        schema: JSON Schema for the response; omitted means free-form JSON.
        schema_name: Name reported to the structured-outputs API.
        strict: Enforce the schema exactly (needs additionalProperties=false
            and every property required, per OpenAI's strict mode).
        model: OpenAI model ID; defaults to $OPENAI_MODEL or DEFAULT_MODEL.
        vision: Also send page images (PDF/image docs only).
        source_bytes: Original file bytes, required when ``vision`` is set.
        client: Injected OpenAI client (tests); built from env otherwise.

    Returns:
        The parsed model output plus token usage.

    Raises:
        LlmExtractionError: Missing API key, API failure, or non-JSON output.
    """
    model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    client = client or _client()

    text = render_document_for_prompt(doc)
    truncated = len(text) > MAX_INPUT_CHARS
    text = text[:MAX_INPUT_CHARS]

    system = DEFAULT_INSTRUCTIONS
    if instructions:
        system += "\n\nTask:\n" + instructions
    if schema is None:
        system += "\n\nRespond with a single JSON object."

    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    if vision and source_bytes is not None:
        user_content += _image_inputs(doc, source_bytes)

    text_format: dict[str, Any] = (
        {"type": "json_schema", "name": schema_name, "schema": schema, "strict": strict}
        if schema is not None
        else {"type": "json_object"}
    )

    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            text={"format": text_format},
        )
    except Exception as e:
        raise LlmExtractionError(f"OpenAI request failed: {e}") from e

    raw = response.output_text
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise LlmExtractionError(f"model returned non-JSON output: {raw!r:.200}") from e

    usage = response.usage.model_dump() if getattr(response, "usage", None) else {}
    return LlmExtraction(model=model, data=data, usage=usage, truncated=truncated)
