from __future__ import annotations

import json
from pathlib import Path

from extraction_service.models import ExtractedDocument
from extraction_service.quote.rfq_line_extract import (
    extract_lines_from_document,
    rfq_number_from_subject,
)


PULLS = Path(__file__).resolve().parents[1] / "app_storage/pulls/jason.bian57@gmail.com"


def test_med_numbered_format():
    path = PULLS / "1a126fef641796b2.json"
    if not path.exists():
        return
    doc = ExtractedDocument.model_validate(json.loads(path.read_text()))
    lines = extract_lines_from_document(doc)
    assert len(lines) >= 40
    first = lines[0]
    assert first["rfq_format"] == "numbered"
    assert first["line_no"] == 1
    assert "MEDICAL MASK" in first["description"]
    assert first["quantity"] == 24
    assert first["unit_of_measure"] == "bx"


def test_aut_spec_ref_format():
    path = PULLS / "1a126fe4b587dee7.json"
    if not path.exists():
        return
    doc = ExtractedDocument.model_validate(json.loads(path.read_text()))
    lines = extract_lines_from_document(doc)
    assert len(lines) >= 20
    assert lines[0]["rfq_format"] == "spec_ref"
    assert lines[0]["buyer_part_no"] == "HB609830"
    assert lines[0]["quantity"] == 20


def test_rfq_number_from_subject():
    assert rfq_number_from_subject("Request for Quotation RFQ-MED-437109") == "MED-437109"
    assert rfq_number_from_subject("RFQ RFQ-AUT-098212") == "AUT-098212"
