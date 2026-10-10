from __future__ import annotations

import json
from pathlib import Path

from extraction_service.models import ExtractedDocument
from extraction_service.quote.document_loader import load_extracted_document


def test_hot_load_prefers_fresh_disk(tmp_path: Path):
    mailbox = "user@example.com"
    mid = "abc123"
    path = tmp_path / mailbox / f"{mid}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"filename": "a.eml", "kind": "email", "text": "v1"}))

    doc1 = load_extracted_document(mailbox, mid, pulls_root=tmp_path)
    assert doc1.text == "v1"

    path.write_text(json.dumps({"filename": "a.eml", "kind": "email", "text": "v2 hot"}))
    doc2 = load_extracted_document(mailbox, mid, pulls_root=tmp_path)
    assert doc2.text == "v2 hot"
