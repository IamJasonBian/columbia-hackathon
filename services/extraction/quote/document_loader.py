"""Hot-load the latest extracted document for a mailbox message."""

from __future__ import annotations

import json
from pathlib import Path

from services.extraction.db.postgres import PostgresSink
from services.extraction.models import ExtractedDocument


def pulls_path(mailbox: str, gmail_message_id: str, pulls_root: Path | None = None) -> Path:
    root = pulls_root or Path(__file__).resolve().parents[3] / "app_storage" / "pulls"
    safe_mailbox = mailbox.strip().lower()
    return root / safe_mailbox / f"{gmail_message_id}.json"


def load_extracted_document(
    mailbox: str,
    gmail_message_id: str,
    *,
    sink: PostgresSink | None = None,
    pulls_root: Path | None = None,
) -> ExtractedDocument:
    """Always re-read disk first (hot load), then fall back to Postgres ``document`` JSON."""
    path = pulls_path(mailbox, gmail_message_id, pulls_root)
    if path.is_file():
        return ExtractedDocument.model_validate(json.loads(path.read_text()))

    if sink is None:
        raise FileNotFoundError(f"no extraction for {mailbox}/{gmail_message_id}")

    raw = sink.get_message(mailbox, gmail_message_id)
    if raw is None:
        raise FileNotFoundError(f"no extraction for {mailbox}/{gmail_message_id}")
    return ExtractedDocument.model_validate(raw)
