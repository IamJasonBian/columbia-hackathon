"""PostgresSink tests (mocked driver, no live DB)."""

from __future__ import annotations

import psycopg

from services.extraction.db.postgres import PostgresSink
from services.extraction.models import DocumentKind, ExtractedDocument


class FakeConn:
    def __init__(self, rows=()):
        self.executed: list[tuple[str, tuple | None]] = []
        self.rows = list(rows)

    def execute(self, sql, params=None):
        self.executed.append((sql.strip().split()[0], params))
        return self

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_write_upserts_message(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: conn)
    sink = PostgresSink("postgresql://local/test")
    sink._schema_ready = True

    sink.write("box@example.com", "m1", ExtractedDocument(filename="m.eml", kind=DocumentKind.email))

    assert [verb for verb, _ in conn.executed] == ["INSERT"]
    assert conn.executed[0][1][:2] == ("box@example.com", "m1")


def test_verify_quote_tables_reports_missing(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConn([("compliance",)]))

    assert PostgresSink("postgresql://local/test").verify_quote_tables() == {"compliance"}
