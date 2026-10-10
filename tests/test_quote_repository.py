from __future__ import annotations

from services.extraction.db.postgres import PostgresSink
from services.extraction.quote.models import ComplianceRecord, LineRecord, RfqQuoteHeader
from services.extraction.quote.repository import QuoteRepository


def test_save_quote_bundle_sql(monkeypatch):
    executed: list[str] = []

    class FakeTx:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    class FakeCur:
        def executemany(self, sql, params):
            executed.append("EXECUTEMANY")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append(sql.strip().split()[0].upper())
            if "RETURNING" in sql.upper():
                return type("R", (), {"fetchone": lambda self: (99,)})()
            return None

        def cursor(self):
            return FakeCur()

        def transaction(self):
            return FakeTx()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConn())

    repo = QuoteRepository(PostgresSink("postgresql://local/test"))
    repo.sink._schema_ready = True
    quote_id = repo.save_quote_bundle(
        RfqQuoteHeader(mailbox="m@x.com", gmail_message_id="mid", has_attachments=True),
        [
            LineRecord(
                line_seq=1,
                line_no=1,
                description="304 SS sheet 16ga 4x8",
                line_status="parsed",
            )
        ],
        [
            ComplianceRecord(
                requirement_code="mtr",
                requirement_label="Mill test report",
                status="required",
            )
        ],
    )
    assert quote_id == 99
    assert "INSERT" in executed
    assert "DELETE" in executed
    assert "EXECUTEMANY" in executed
