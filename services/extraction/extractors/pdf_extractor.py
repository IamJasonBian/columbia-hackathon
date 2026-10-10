"""PDF extraction: layout-preserved text, tables, and OCR for scanned pages.

Text uses pdfplumber's ``extract_text(layout=True)`` so table columns stay
aligned. Pages with no text layer are rendered and OCR'd through
services.extraction.ocr, so a scanned PDF and a scanned TIFF come out the same.
"""

from __future__ import annotations

import io

import pdfplumber
from PIL import Image

from services.extraction import ocr
from services.extraction.models import DocumentKind, ExtractedDocument, ExtractedPage, ExtractedTable

# Render resolution for scanned pages (Tesseract's sweet spot is 300 DPI).
OCR_RENDER_DPI = 300

# A page with fewer non-whitespace chars than this is treated as image-only.
MIN_TEXT_LAYER_CHARS = 20


def render_pdf_pages(data: bytes, resolution: int = OCR_RENDER_DPI) -> list[Image.Image]:
    """Rasterize every PDF page, DPI-tagged for the OCR layer.

    Args:
        data: Raw PDF bytes.
        resolution: Render DPI.

    Returns:
        One PIL image per page.
    """
    images: list[Image.Image] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            image = page.to_image(resolution=resolution).original.copy()
            image.info["dpi"] = (resolution, resolution)
            images.append(image)
    return images


def _clean_cell(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def extract_pdf(data: bytes, filename: str, *, enable_ocr: bool = True) -> ExtractedDocument:
    """Extract text, tables, and metadata from a PDF.

    Args:
        data: Raw PDF bytes.
        filename: Original filename (for the output record).
        enable_ocr: OCR pages that have no usable text layer.

    Returns:
        The extracted document.
    """
    doc = ExtractedDocument(filename=filename, kind=DocumentKind.pdf, content_type="application/pdf")
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:
        doc.warnings.append(f"could not open PDF: {e}")
        return doc

    with pdf:
        doc.metadata = {k: str(v) for k, v in (pdf.metadata or {}).items()}
        doc.metadata["page_count"] = len(pdf.pages)
        for index, page in enumerate(pdf.pages):
            text = page.extract_text(layout=True) or ""
            used_ocr = False
            if len("".join(text.split())) < MIN_TEXT_LAYER_CHARS and enable_ocr:
                try:
                    image = page.to_image(resolution=OCR_RENDER_DPI).original.copy()
                    image.info["dpi"] = (OCR_RENDER_DPI, OCR_RENDER_DPI)
                    text = ocr.ocr_page(image)
                    used_ocr = True
                except ocr.OcrError as e:
                    doc.warnings.append(f"page {index}: OCR failed: {e}")
            doc.pages.append(ExtractedPage(index=index, text=text, ocr=used_ocr))

            for table_index, table in enumerate(page.extract_tables() or []):
                rows = [[_clean_cell(c) for c in row] for row in table if any(row)]
                if not rows:
                    continue
                headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
                doc.tables.append(
                    ExtractedTable(
                        name=f"page {index + 1} table {table_index + 1}",
                        page=index,
                        headers=headers,
                        rows=rows[1:],
                    )
                )

    doc.text = "\f".join(p.text for p in doc.pages)
    if any(p.ocr for p in doc.pages):
        doc.metadata["ocr_pages"] = [p.index for p in doc.pages if p.ocr]
    return doc
