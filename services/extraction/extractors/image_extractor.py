"""Image extraction (TIFF/PNG/JPEG): OCR every frame via services.extraction.ocr."""

from __future__ import annotations

import io

from PIL import Image, ImageSequence

from services.extraction import ocr
from services.extraction.models import DocumentKind, ExtractedDocument, ExtractedPage


def load_image_pages(data: bytes) -> list[Image.Image]:
    """Load every frame of a (possibly multi-page) image.

    Args:
        data: Raw image bytes.

    Returns:
        One materialized PIL image per frame.

    Raises:
        ocr.OcrError: The bytes are not a readable image.
    """
    try:
        image = Image.open(io.BytesIO(data))
        return [frame.copy() for frame in ImageSequence.Iterator(image)]
    except Exception as e:
        raise ocr.OcrError(f"could not open image: {e}") from e


def extract_image(data: bytes, filename: str, *, enable_ocr: bool = True) -> ExtractedDocument:
    """OCR an image file page by page.

    Args:
        data: Raw image bytes.
        filename: Original filename.
        enable_ocr: When False, only frame metadata is returned.

    Returns:
        The extracted document.
    """
    doc = ExtractedDocument(filename=filename, kind=DocumentKind.image)
    try:
        pages = load_image_pages(data)
    except ocr.OcrError as e:
        doc.warnings.append(str(e))
        return doc
    doc.metadata = {
        "page_count": len(pages),
        "size": list(pages[0].size),
        "dpi": list(pages[0].info.get("dpi", ())) or None,
    }
    for index, page in enumerate(pages):
        text = ""
        if enable_ocr:
            try:
                text = ocr.ocr_page(page)
            except ocr.OcrError as e:
                doc.warnings.append(f"page {index}: OCR failed: {e}")
        doc.pages.append(ExtractedPage(index=index, text=text, ocr=enable_ocr))
    doc.text = "\f".join(p.text for p in doc.pages)
    return doc
