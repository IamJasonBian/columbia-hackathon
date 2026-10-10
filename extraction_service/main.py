"""Extraction service: email / PDF / spreadsheet / image -> text, tables, JSON."""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile

from extraction_service.db.postgres import PostgresSink, database_url_from_env
from extraction_service.extractors import extract_document
from extraction_service.llm import LlmExtractionError, extract_structured
from extraction_service.models import DocumentKind, ExtractedDocument
from extraction_service.nicrron import NicrronExtractionError, classify_document
from extraction_service.ocr import ocr_available
from shared.auth import require_bearer
from shared.crud import CrudStore

app = FastAPI(title="Extraction Service", version="0.1.0")
logger = logging.getLogger(__name__)
store = CrudStore[dict](dict, prefix="ext")  # type: ignore[type-var]

_MAILBOX_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")

MAX_UPLOAD_BYTES = int(os.environ.get("EXTRACTION_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_BYTES} bytes")
    return data


def _save(doc: ExtractedDocument, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    return store.create(
        {
            "filename": doc.filename,
            "kind": doc.kind.value,
            "document": doc.model_dump(mode="json"),
            "structured": structured,
        }
    )


async def _extract(file: UploadFile, ocr: bool, kind: DocumentKind | None) -> dict[str, Any]:
    data = await _read_upload(file)
    doc = extract_document(
        data, file.filename or "upload", file.content_type, enable_ocr=ocr, kind=kind
    )
    return _save(doc)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "extraction",
        "ocr_available": ocr_available(),
        "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "nicrron_configured": bool(os.environ.get("NICRRON_API_KEY")),
    }


@app.post("/extract", dependencies=[Depends(require_bearer)])
async def extract_auto(file: UploadFile = File(...), ocr: bool = Query(True)) -> dict[str, Any]:
    """Auto-detect the format and extract."""
    return await _extract(file, ocr, None)


@app.post("/extract/email", dependencies=[Depends(require_bearer)])
async def extract_email(file: UploadFile = File(...), ocr: bool = Query(True)) -> dict[str, Any]:
    """Extract a raw .eml (headers, body, and every attachment)."""
    return await _extract(file, ocr, DocumentKind.email)


@app.post("/extract/pdf", dependencies=[Depends(require_bearer)])
async def extract_pdf(file: UploadFile = File(...), ocr: bool = Query(True)) -> dict[str, Any]:
    """Extract PDF text and tables, OCR'ing pages with no text layer."""
    return await _extract(file, ocr, DocumentKind.pdf)


@app.post("/extract/spreadsheet", dependencies=[Depends(require_bearer)])
async def extract_spreadsheet(file: UploadFile = File(...)) -> dict[str, Any]:
    """Extract every sheet of an .xlsx, or a .csv/.tsv."""
    return await _extract(file, False, DocumentKind.spreadsheet)


@app.post("/extract/structured", dependencies=[Depends(require_bearer)])
async def extract_structured_endpoint(
    file: UploadFile = File(...),
    instructions: str | None = Form(None),
    json_schema: str | None = Form(None, description="JSON Schema as a JSON string"),
    strict: bool = Form(False),
    model: str | None = Form(None),
    vision: bool = Form(False),
    ocr: bool = Form(True),
) -> dict[str, Any]:
    """Extract the file, then have OpenAI return JSON (optionally schema-bound)."""
    data = await _read_upload(file)
    schema = None
    if json_schema:
        try:
            schema = json.loads(json_schema)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"json_schema is not valid JSON: {e}") from e
    doc = extract_document(data, file.filename or "upload", file.content_type, enable_ocr=ocr)
    try:
        result = extract_structured(
            doc,
            instructions=instructions,
            schema=schema,
            strict=strict,
            model=model,
            vision=vision,
            source_bytes=data,
        )
    except LlmExtractionError as e:
        raise HTTPException(502, str(e)) from e
    return _save(doc, result.model_dump(mode="json"))


@app.post("/extract/nicrron", dependencies=[Depends(require_bearer)])
async def classify_with_nicrron_endpoint(
    file: UploadFile = File(...),
    labels: str = Form(..., description="JSON array of allowed classification labels"),
    instructions: str | None = Form(None),
    model: str | None = Form(None),
    ocr: bool = Form(True),
) -> dict[str, Any]:
    """Extract a document, then classify it through Nicrron."""
    try:
        parsed_labels = json.loads(labels)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "labels must be a JSON array of strings") from exc
    if (
        not isinstance(parsed_labels, list)
        or not parsed_labels
        or not all(isinstance(label, str) and label.strip() for label in parsed_labels)
    ):
        raise HTTPException(400, "labels must be a non-empty JSON array of strings")

    data = await _read_upload(file)
    doc = extract_document(
        data,
        file.filename or "upload",
        file.content_type,
        enable_ocr=ocr,
    )
    try:
        result = classify_document(
            doc,
            parsed_labels,
            instructions=instructions,
            model=model,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except NicrronExtractionError as exc:
        logger.warning("Nicrron classification unavailable for %s: %s", doc.filename, exc)
        classification = None
    else:
        classification = result.model_dump(mode="json")
    return _save(
        doc,
        {
            "provider": "nicrron",
            "classification": classification,
        },
    )


def _mailbox_name(mailbox: str) -> str:
    if not _MAILBOX_RE.match(mailbox):
        raise HTTPException(400, "invalid mailbox")
    return mailbox.lower()


@lru_cache(maxsize=1)
def _mailbox_store() -> PostgresSink:
    url = database_url_from_env()
    if not url:
        raise HTTPException(503, "PostgreSQL is not configured")
    return PostgresSink(url)


@app.get("/pulls", dependencies=[Depends(require_bearer)])
def list_polled_mailboxes() -> list[str]:
    """Mailboxes persisted by the poller."""
    return _mailbox_store().list_mailboxes()


@app.get("/pulls/{mailbox}/state", dependencies=[Depends(require_bearer)])
def poll_state(mailbox: str) -> dict[str, Any]:
    """The poller's durable PostgreSQL state for a mailbox."""
    row = _mailbox_store().poll_state(_mailbox_name(mailbox))
    if row is None:
        raise HTTPException(404, "mailbox not found")
    return row


@app.get("/pulls/{mailbox}/messages", dependencies=[Depends(require_bearer)])
def poll_messages(
    mailbox: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, Any]]:
    """One summary row per message the poller extracted, newest first."""
    name = _mailbox_name(mailbox)
    if _mailbox_store().poll_state(name) is None:
        raise HTTPException(404, "mailbox not found")
    return _mailbox_store().list_messages(name, limit)


@app.get("/pulls/{mailbox}/messages/{message_id}", dependencies=[Depends(require_bearer)])
def poll_message(mailbox: str, message_id: str) -> dict[str, Any]:
    """Full extraction JSON for one polled message."""
    if not re.fullmatch(r"[A-Za-z0-9]+", message_id):
        raise HTTPException(400, "invalid message id")
    row = _mailbox_store().get_message(_mailbox_name(mailbox), message_id)
    if row is None:
        raise HTTPException(404, "message not found")
    return row


@app.get("/extractions", dependencies=[Depends(require_bearer)])
def list_extractions(
    kind: DocumentKind | None = Query(None), limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    def _filter(row: dict[str, Any]) -> bool:
        return kind is None or row.get("kind") == kind.value

    return store.list(limit=limit, offset=offset, filter_fn=_filter)


@app.get("/extractions/{extraction_id}", dependencies=[Depends(require_bearer)])
def get_extraction(extraction_id: str) -> dict[str, Any]:
    row = store.get(extraction_id)
    if not row:
        raise HTTPException(404, "extraction not found")
    return row


@app.delete("/extractions/{extraction_id}", dependencies=[Depends(require_bearer)])
def delete_extraction(extraction_id: str) -> dict[str, bool]:
    if not store.delete(extraction_id):
        raise HTTPException(404, "extraction not found")
    return {"deleted": True}


def run() -> None:
    import uvicorn

    port = int(os.environ.get("EXTRACTION_PORT", "8012"))
    uvicorn.run("extraction_service.main:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    run()
