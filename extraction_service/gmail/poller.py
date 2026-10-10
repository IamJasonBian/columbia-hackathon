"""Poll a connected mailbox and extract every new message with a document.

Each tick runs one cheap ``messages.list`` search (5 quota units) bounded by
an ``after:`` epoch, skips IDs already processed, and for each new message
fetches the raw RFC 822 and runs extract_document on it — so PDFs,
spreadsheets, images, and forwarded .eml attachments all go through the same
extractors as a manual upload. Processed IDs and documents persist through a
pluggable store; production uses PostgreSQL and offline tests can use files.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from extraction_service.db.postgres import PostgresSink
from extraction_service.extractors import extract_document
from extraction_service.gmail.client import GmailClient
from extraction_service.gmail.oauth import GmailCredentials, access_token
from extraction_service.models import ExtractedDocument

logger = logging.getLogger(__name__)

DOCUMENT_QUERY = (
    "has:attachment {filename:pdf filename:xlsx filename:xlsm filename:xls "
    "filename:csv filename:tsv filename:tif filename:tiff filename:png "
    "filename:jpg filename:jpeg filename:eml}"
)

# Google access tokens last 3600s; refresh a little early.
TOKEN_TTL_SECONDS = 50 * 60


@dataclass
class PollState:
    after_epoch: int
    seen_ids: list[str] = field(default_factory=list)


class MailboxStore(Protocol):
    def load_state(self, lookback_seconds: int) -> PollState: ...
    def save_state(self, state: PollState) -> None: ...
    def save_document(self, message_id: str, doc: ExtractedDocument) -> None: ...


class FileMailboxStore:
    """Legacy local persistence for offline runs and tests."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out_dir / "_state.json"

    def load_state(self, lookback_seconds: int) -> PollState:
        if self.state_path.exists():
            return PollState(**json.loads(self.state_path.read_text()))
        return PollState(after_epoch=int(time.time()) - lookback_seconds)

    def save_state(self, state: PollState) -> None:
        state.seen_ids = state.seen_ids[-5000:]
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.__dict__))
        tmp.replace(self.state_path)

    def save_document(self, message_id: str, doc: ExtractedDocument) -> None:
        (self.out_dir / f"{message_id}.json").write_text(
            json.dumps(doc.model_dump(mode="json"), indent=2)
        )


class PostgresMailboxStore:
    """PostgreSQL-backed mailbox state and extracted documents."""

    def __init__(self, sink: PostgresSink, mailbox: str):
        self.sink = sink
        self.mailbox = mailbox.lower()

    def load_state(self, lookback_seconds: int) -> PollState:
        after_epoch, seen_ids = self.sink.load_poll_state(self.mailbox, lookback_seconds)
        return PollState(after_epoch=after_epoch, seen_ids=seen_ids)

    def save_state(self, state: PollState) -> None:
        self.sink.save_poll_state(self.mailbox, state.after_epoch)

    def save_document(self, message_id: str, doc: ExtractedDocument) -> None:
        self.sink.write(self.mailbox, message_id, doc)


class MailboxPoller:
    """Incremental extractor for one mailbox."""

    def __init__(
        self,
        creds: GmailCredentials,
        out_dir: Path | None = None,
        *,
        store: MailboxStore | None = None,
        query: str = DOCUMENT_QUERY,
        lookback_seconds: int = 24 * 3600,
        enable_ocr: bool = True,
        max_per_tick: int = 25,
        on_document: Callable[[str, ExtractedDocument], None] | None = None,
        on_extracted: Callable[[str, ExtractedDocument], None] | None = None,
        client_factory: Callable[[str], GmailClient] = GmailClient,
    ):
        self.creds = creds
        self.query = query
        self.enable_ocr = enable_ocr
        self.max_per_tick = max_per_tick
        self.on_document = on_document
        self.on_extracted = on_extracted
        self._client_factory = client_factory
        self._client: GmailClient | None = None
        self._token_minted_at = 0.0
        if store is None:
            if out_dir is None:
                raise ValueError("out_dir or store is required")
            store = FileMailboxStore(out_dir)
        self.store = store
        self.state = self.store.load_state(lookback_seconds)

    def _gmail(self) -> GmailClient:
        if self._client is None or time.time() - self._token_minted_at > TOKEN_TTL_SECONDS:
            self._client = self._client_factory(access_token(self.creds))
            self._token_minted_at = time.time()
        return self._client

    def poll_once(self) -> list[tuple[str, ExtractedDocument]]:
        """Process every not-yet-seen message matching the query.

        Returns:
            (message_id, document) for each message extracted this tick.
        """
        gmail = self._gmail()
        query = f"{self.query} after:{self.state.after_epoch}"
        seen = set(self.state.seen_ids)
        new_ids = [i for i in gmail.search(query, self.max_per_tick) if i not in seen]
        results: list[tuple[str, ExtractedDocument]] = []
        # Oldest first, so the state file advances in arrival order.
        for message_id in reversed(new_ids):
            try:
                raw = gmail.get_raw(message_id)
                doc = extract_document(
                    raw, f"{message_id}.eml", "message/rfc822", enable_ocr=self.enable_ocr
                )
            except Exception:
                logger.exception("extraction failed for %s; will retry next tick", message_id)
                continue
            try:
                self.store.save_document(message_id, doc)
                if self.on_extracted:
                    self.on_extracted(message_id, doc)
                if self.on_document:
                    self.on_document(message_id, doc)
            except Exception:
                logger.exception("document sink failed for %s; will retry next tick", message_id)
                continue
            self.state.seen_ids.append(message_id)
            self.store.save_state(self.state)
            results.append((message_id, doc))
        return results

    def run(self, interval_seconds: float = 10.0, stop: Callable[[], bool] = lambda: False) -> None:
        """Poll forever (or until ``stop()``), surviving transient errors.

        Args:
            interval_seconds: Sleep between ticks.
            stop: Checked before each tick; return True to exit.
        """
        while not stop():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                # Network blips / 5xx / expired token: drop the client so the
                # next tick re-mints, and keep polling.
                logger.exception("poll tick failed")
                self._client = None
            time.sleep(max(0.0, interval_seconds - (time.monotonic() - started)))
