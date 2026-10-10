"""Tesseract OCR for page images (scanned PDFs, TIFFs, PNG/JPEG).

Each page is OCR'd into a DPI-tagged single-page PDF with an invisible text
layer (``pytesseract.image_to_pdf_or_hocr``) and then read back with
pdfplumber's layout-preserving text extraction. That keeps table columns
aligned far better than pytesseract's raw string output. The DPI tag matters:
without it Tesseract maps pixels 1:1 to PDF points and pdfplumber's layout
spacing breaks down.
"""

from __future__ import annotations

import io

import pdfplumber
from PIL import Image, ImageOps

try:
    import pytesseract
except ImportError:  # pragma: no cover — surfaced lazily in ocr_page()
    pytesseract = None

# "Assume a single uniform block of text" — suits tables and forms.
PAGE_SEGMENTATION_MODE = "--psm 6"

# Used only when the image doesn't declare its own DPI.
DEFAULT_DPI = 300


class OcrError(Exception):
    """A page image could not be OCR'd."""


def page_dpi(image: Image.Image) -> int:
    """The image's declared DPI, or DEFAULT_DPI.

    Args:
        image: A page image.

    Returns:
        Horizontal DPI as an int.
    """
    dpi = image.info.get("dpi")
    if dpi and dpi[0]:
        return round(dpi[0])
    return DEFAULT_DPI


def ocr_available() -> bool:
    """True when pytesseract and the tesseract binary are both usable.

    Returns:
        Whether OCR can run in this environment.
    """
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


def ocr_page(image: Image.Image) -> str:
    """OCR one page image to layout-preserved text.

    Args:
        image: A page image (any mode; converted to grayscale).

    Returns:
        The page text.

    Raises:
        OcrError: Tesseract is missing or failed on this page.
    """
    if pytesseract is None:
        raise OcrError("pytesseract is not installed")
    grayscale = ImageOps.grayscale(image)
    dpi = page_dpi(image)
    grayscale.info["dpi"] = (dpi, dpi)
    try:
        pdf_bytes = pytesseract.image_to_pdf_or_hocr(
            grayscale, extension="pdf", config=f"{PAGE_SEGMENTATION_MODE} --dpi {dpi}"
        )
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return pdf.pages[0].extract_text(layout=True) or ""
    except Exception as e:
        raise OcrError(f"OCR failed: {e}") from e
