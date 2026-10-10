from __future__ import annotations

import json
from pathlib import Path

from services.extraction.models import ExtractedDocument
from services.extraction.quote.rfq_line_extract import extract_lines_from_document


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
