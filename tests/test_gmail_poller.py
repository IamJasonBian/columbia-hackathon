"""MailboxPoller tests with a fake Gmail client (no network)."""

from __future__ import annotations

import json

from extraction_service.gmail import poller as poller_mod
from extraction_service.gmail.oauth import GmailCredentials
from extraction_service.gmail.poller import MailboxPoller, PostgresMailboxStore
from tests.test_extraction_service import make_eml, make_text_pdf

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


def test_state_survives_restart(tmp_path, monkeypatch):
    fake = FakeGmail({"m1": _pdf_mail()})
    _poller(tmp_path, fake, monkeypatch).poll_once()
    restarted = _poller(tmp_path, fake, monkeypatch)
    assert restarted.poll_once() == []
    assert restarted.state.seen_ids == ["m1"]


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


def test_failed_on_extracted_is_retried(tmp_path, monkeypatch):
    fake = FakeGmail({"m1": _pdf_mail()})
    calls = 0

    def ingest(message_id, doc):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("postgres down")

    p = _poller(tmp_path, fake, monkeypatch, on_extracted=ingest)
    assert p.poll_once() == []
    assert "m1" not in p.state.seen_ids
    assert [mid for mid, _ in p.poll_once()] == ["m1"]
    assert calls == 2


def test_failed_document_callback_is_retried(tmp_path, monkeypatch):
    fake = FakeGmail({"m1": _pdf_mail()})
    calls = 0

    def write_document(message_id, doc):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database unavailable")

    p = _poller(tmp_path, fake, monkeypatch, on_document=write_document)
    assert p.poll_once() == []
    assert "m1" not in p.state.seen_ids
    assert [mid for mid, _ in p.poll_once()] == ["m1"]
    assert calls == 2


def test_run_stops_and_survives_tick_errors(tmp_path, monkeypatch):
    fake = FakeGmail({"m1": _pdf_mail()})
    p = _poller(tmp_path, fake, monkeypatch)
    calls = {"n": 0}
    real = p.poll_once

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("blip")
        return real()

    p.poll_once = flaky  # type: ignore[method-assign]
    p.run(interval_seconds=0, stop=lambda: calls["n"] >= 2)
    assert (tmp_path / "m1.json").exists()


class FakeMailboxRepository:
    def __init__(self, doc):
        self.doc = doc.model_dump(mode="json")

    def list_mailboxes(self):
        return ["box@example.com"]

    def poll_state(self, mailbox):
        if mailbox != "box@example.com":
            return None
        return {"after_epoch": 1, "processed_count": 1}

    def list_messages(self, mailbox, limit=100):
        meta = self.doc["metadata"]
        return [{
            "message_id": "m1",
            "date": meta["date"],
            "from": meta["from"],
            "subject": meta["subject"],
            "attachments": [
                {"filename": a["filename"], "kind": a["kind"], "chars": len(a["text"])}
                for a in self.doc["attachments"]
            ],
        }]

    def get_message(self, mailbox, message_id):
        return self.doc if mailbox == "box@example.com" and message_id == "m1" else None


def _fake_mailbox_repository(tmp_path, monkeypatch):
    from extraction_service import main

    result = _poller(tmp_path, FakeGmail({"m1": _pdf_mail()}), monkeypatch).poll_once()
    repository = FakeMailboxRepository(result[0][1])
    monkeypatch.setattr(main, "_mailbox_store", lambda: repository)
    return repository


def test_pulls_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from extraction_service import main

    _fake_mailbox_repository(tmp_path, monkeypatch)

    client = TestClient(main.app)
    assert client.get("/pulls").json() == ["box@example.com"]
    assert client.get("/pulls/box@example.com/state").json()["processed_count"] == 1
    rows = client.get("/pulls/box@example.com/messages").json()
    assert rows[0]["message_id"] == "m1" and rows[0]["attachments"][0]["kind"] == "pdf"
    assert client.get("/pulls/box@example.com/messages/m1").json()["kind"] == "email"
    assert client.get("/pulls/..%2Fetc/state").status_code in (400, 404)
    assert client.get("/pulls/nobody@example.com/state").status_code == 404


def test_public_view_requires_token_and_hides_full_text(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from extraction_service import main, public_view

    monkeypatch.setenv("PUBLIC_VIEW_TOKEN", "s3cret")
    _fake_mailbox_repository(tmp_path, monkeypatch)

    client = TestClient(public_view.app)
    assert client.get("/pulls/box@example.com/state").status_code == 401
    assert client.get("/pulls/box@example.com/state?token=wrong").status_code == 401
    auth = {"Authorization": "Bearer s3cret"}
    ok = client.get("/pulls/box@example.com/state", headers=auth)
    assert ok.json()["processed_count"] == 1
    rows = client.get("/pulls/box@example.com/messages", headers=auth).json()
    assert rows[0]["subject"] and "text" not in rows[0]
    # Full-extraction and upload routes do not exist on the public app.
    assert client.get("/pulls/box@example.com/messages/m1", headers=auth).status_code == 404
    assert client.post("/extract", headers=auth).status_code in (404, 405)
    assert client.get("/docs", headers=auth).status_code == 404


def test_public_view_serves_filtered_postgres_logs(monkeypatch):
    from fastapi.testclient import TestClient

    from extraction_service import public_view

    class FakeLogStore:
        def list(self, *, service, after_id, limit):
            assert (service, after_id, limit) == ("core", 10, 25)
            return [{"id": 11, "service": "core", "level": "INFO", "message": "ready"}]

    monkeypatch.setenv("PUBLIC_VIEW_TOKEN", "s3cret")
    monkeypatch.setattr(public_view, "_log_store", lambda: FakeLogStore())

    response = TestClient(public_view.app).get(
        "/logs?service=core&after_id=10&limit=25",
        headers={"Authorization": "Bearer s3cret"},
    )

    assert response.status_code == 200
    assert response.json() == [
        {"id": 11, "service": "core", "level": "INFO", "message": "ready"}
    ]
