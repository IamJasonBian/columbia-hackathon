"""Format detection + dispatch to the per-format extractors."""

from __future__ import annotations

import hashlib

from services.extraction.extractors.email_extractor import FetchedMessage, looks_like_eml, parse_eml
from services.extraction.extractors.image_extractor import extract_image
from services.extraction.extractors.pdf_extractor import extract_pdf
from services.extraction.extractors.spreadsheet_extractor import (
    CSV_EXTENSIONS,
    XLSX_EXTENSIONS,
    extract_spreadsheet,
)
from services.extraction.models import DocumentKind, ExtractedDocument

IMAGE_EXTENSIONS = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")

# Forwarded emails can nest; bound the recursion so a malicious or looping
# message can't blow the stack.
MAX_ATTACHMENT_DEPTH = 3


def detect_kind(filename: str, data: bytes, content_type: str | None = None) -> DocumentKind:
    """Classify a file by magic bytes, then extension, then content type.

    Args:
        filename: Original filename.
        data: Raw bytes.
        content_type: Declared MIME type, if any.

    Returns:
        The detected document kind.
    """
    lower = filename.lower()
    ctype = (content_type or "").lower()
    if data[:5] == b"%PDF-":
        return DocumentKind.pdf
    if data[:4] in (b"II*\x00", b"MM\x00*") or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        return DocumentKind.image
    if lower.endswith(XLSX_EXTENSIONS + (".xls",)) or "spreadsheet" in ctype or "excel" in ctype:
        return DocumentKind.spreadsheet
    if lower.endswith(".eml") or ctype == "message/rfc822":
        return DocumentKind.email
    if lower.endswith(IMAGE_EXTENSIONS) or ctype.startswith("image/"):
        return DocumentKind.image
    if lower.endswith((".csv", ".tsv")) or ctype in ("text/csv", "text/tab-separated-values"):
        return DocumentKind.spreadsheet
    if looks_like_eml(data):
        return DocumentKind.email
    if lower.endswith(CSV_EXTENSIONS) or ctype.startswith("text/"):
        return DocumentKind.text
    return DocumentKind.unknown


def extract_email(
    data: bytes, filename: str, *, enable_ocr: bool = True, _depth: int = 0
) -> ExtractedDocument:
    """Extract an .eml: headers, body text, and every attachment recursively.

    Args:
        data: Raw RFC 822 bytes.
        filename: Original filename.
        enable_ocr: OCR scanned attachments.

    Returns:
        The extracted document; attachments live in ``attachments``.
    """
    message = parse_eml(data, fallback_id=hashlib.sha256(data).hexdigest()[:16])
    doc = email_message_to_document(message, filename)
    for attachment in message.attachments:
        if _depth >= MAX_ATTACHMENT_DEPTH:
            doc.warnings.append(f"skipped '{attachment.filename}': attachment nesting too deep")
            continue
        doc.attachments.append(
            extract_document(
                attachment.data,
                attachment.filename,
                attachment.content_type,
                enable_ocr=enable_ocr,
                _depth=_depth + 1,
            )
        )
    return doc


def email_message_to_document(message: FetchedMessage, filename: str) -> ExtractedDocument:
    """Map a parsed message's envelope + body onto an ExtractedDocument.

    Args:
        message: The parsed message.
        filename: Original filename.

    Returns:
        The document, without attachments extracted.
    """
    return ExtractedDocument(
        filename=filename,
        kind=DocumentKind.email,
        content_type="message/rfc822",
        text=message.body_text,
        metadata={
            "message_id": message.message_id,
            "subject": message.subject,
            "from": message.sender,
            "to": message.recipients,
            "cc": message.cc,
            "date": message.date_iso,
            "attachment_filenames": [a.filename for a in message.attachments],
        },
    )


def extract_document(
    data: bytes,
    filename: str,
    content_type: str | None = None,
    *,
    enable_ocr: bool = True,
    kind: DocumentKind | None = None,
    _depth: int = 0,
) -> ExtractedDocument:
    """Detect a file's format and run the matching extractor.

    Args:
        data: Raw bytes.
        filename: Original filename.
        content_type: Declared MIME type, if any.
        enable_ocr: OCR scanned PDFs and images.
        kind: Force a specific extractor instead of auto-detecting.

    Returns:
        The extracted document, stamped with size and sha256.
    """
    kind = kind or detect_kind(filename, data, content_type)
    if kind == DocumentKind.email:
        doc = extract_email(data, filename, enable_ocr=enable_ocr, _depth=_depth)
    elif kind == DocumentKind.pdf:
        doc = extract_pdf(data, filename, enable_ocr=enable_ocr)
    elif kind == DocumentKind.spreadsheet:
        doc = extract_spreadsheet(data, filename)
    elif kind == DocumentKind.image:
        doc = extract_image(data, filename, enable_ocr=enable_ocr)
    elif kind == DocumentKind.text:
        doc = ExtractedDocument(
            filename=filename, kind=kind, text=data.decode("utf-8", errors="replace")
        )
    else:
        doc = ExtractedDocument(filename=filename, kind=DocumentKind.unknown)
        doc.warnings.append("unrecognized file type; nothing extracted")
    doc.content_type = content_type or doc.content_type
    doc.size_bytes = len(data)
    doc.sha256 = hashlib.sha256(data).hexdigest()
    return doc


__all__ = ["FetchedMessage", "detect_kind", "extract_document", "extract_email", "parse_eml"]
