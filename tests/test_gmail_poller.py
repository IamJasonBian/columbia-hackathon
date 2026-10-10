"""MailboxPoller tests with a fake Gmail client (no network)."""

from __future__ import annotations

import json

from services.extraction.gmail import poller as poller_mod
from services.extraction.gmail.oauth import GmailCredentials
from services.extraction.gmail.poller import MailboxPoller, PostgresMailboxStore
from tests.test_extractors import make_eml, make_text_pdf

CREDS = GmailCredentials(email="box@example.com", client_id="c", client_secret="s", refresh_token="r")


class FakeGmail:
    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.queries: list[str] = []

    def search(self, query: str, max_messages: int = 25) -> list[str]:
        self.queries.append(query)
        return list(reversed(self.messages))[:max_messages]  # newest first

    def get_raw(self, message_id: str) -> bytes:
        if self.messages[message_id] is None:
            raise RuntimeError("boom")
        return self.messages[message_id]


def _poller(tmp_path, fake, monkeypatch, **kw) -> MailboxPoller:
    monkeypatch.setattr(poller_mod, "access_token", lambda creds: "tok")
    return MailboxPoller(CREDS, tmp_path, client_factory=lambda token: fake, **kw)


def _pdf_mail() -> bytes:
    return make_eml([("rfq.pdf", make_text_pdf(["RFQ 1 closing 18 June"]), "application", "pdf")])


def test_poll_extracts_new_and_skips_seen(tmp_path, monkeypatch):
    fake = FakeGmail({"m1": _pdf_mail()})
    seen: list[str] = []
    p = _poller(tmp_path, fake, monkeypatch, on_document=lambda mid, doc: seen.append(mid))

    first = p.poll_once()
    assert [mid for mid, _ in first] == ["m1"]
    assert "RFQ 1" in first[0][1].attachments[0].text
    assert json.loads((tmp_path / "m1.json").read_text())["attachments"][0]["kind"] == "pdf"
    assert "after:" in fake.queries[0] and "filename:pdf" in fake.queries[0]

    fake.messages["m2"] = _pdf_mail()
    second = p.poll_once()
    assert [mid for mid, _ in second] == ["m2"]
    assert p.poll_once() == []
    assert seen == ["m1", "m2"]


def test_postgres_store_persists_without_files(monkeypatch):
    class FakeSink:
        def __init__(self):
            self.documents = {}
            self.after_epoch = 123

        def load_poll_state(self, mailbox, lookback_seconds):
            return self.after_epoch, list(self.documents)

        def save_poll_state(self, mailbox, after_epoch):
            self.after_epoch = after_epoch

        def write(self, mailbox, message_id, doc):
            self.documents[message_id] = doc

    monkeypatch.setattr(poller_mod, "access_token", lambda creds: "tok")
    sink = FakeSink()
    poller = MailboxPoller(
        CREDS,
        store=PostgresMailboxStore(sink, CREDS.email),  # type: ignore[arg-type]
        client_factory=lambda token: FakeGmail({"m1": _pdf_mail()}),
    )

    assert [mid for mid, _ in poller.poll_once()] == ["m1"]
    assert list(sink.documents) == ["m1"]


def test_failed_message_is_retried_next_tick(tmp_path, monkeypatch):
    fake = FakeGmail({"bad": None, "ok": _pdf_mail()})
    p = _poller(tmp_path, fake, monkeypatch)
    assert [mid for mid, _ in p.poll_once()] == ["ok"]
    fake.messages["bad"] = _pdf_mail()
    assert [mid for mid, _ in p.poll_once()] == ["bad"]
