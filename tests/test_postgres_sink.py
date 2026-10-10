"""PostgresSink tests (mocked driver, no live DB)."""

from __future__ import annotations

from extraction_service.db.postgres import PostgresSink, _SCHEMA, iter_table_records
from extraction_service.models import DocumentKind, ExtractedDocument, ExtractedTable


def test_iter_table_records_nested():
    pdf = ExtractedDocument(
        filename="a.pdf",
        kind=DocumentKind.pdf,
        tables=[
            ExtractedTable(name="pricing", headers=["item", "qty"], rows=[["Full page", 6]]),
        ],
    )
    eml = ExtractedDocument(
        filename="m.eml",
        kind=DocumentKind.email,
        attachments=[pdf],
    )
    rows = list(iter_table_records(eml))
    assert len(rows) == 1
    assert rows[0][0] == "a.pdf"
    assert rows[0][4] == {"item": "Full page", "qty": 6}


def test_write_executes_upsert_and_row_inserts(monkeypatch):
    import psycopg

    executed: list[tuple[str, tuple | None]] = []

    class FakeTx:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    class FakeCur:
        def executemany(self, sql, params):
            executed.append(("executemany", params))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((sql.strip().split()[0], params))

        def cursor(self):
            return FakeCur()

        def transaction(self):
            return FakeTx()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def commit(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConn())

    sink = PostgresSink("postgresql://local/test")
    sink._schema_ready = True
    doc = ExtractedDocument(
        filename="1.eml",
        kind=DocumentKind.email,
        metadata={"subject": "RFQ", "from": "a@b.co", "date": "Sat, 10 Oct 2026 11:47:55 -0400"},
        tables=[ExtractedTable(headers=["col"], rows=[["v"]])],
    )
    sink.write("box@example.com", "mid1", doc)

    assert executed[0][0] == "INSERT"
    assert executed[1][0] == "DELETE"
    assert executed[2][0] == "executemany"
    assert len(executed[2][1]) == 1


def test_quote_schema_defines_line_items_and_compliance_tables():
    assert "CREATE TABLE IF NOT EXISTS line_items" in _SCHEMA
    assert "CREATE TABLE IF NOT EXISTS compliance" in _SCHEMA
    assert "quote_id BIGINT NOT NULL REFERENCES rfq_quotes(id) ON DELETE CASCADE" in _SCHEMA
    assert "line_item_id BIGINT REFERENCES line_items(id) ON DELETE CASCADE" in _SCHEMA


def test_verify_quote_tables_reports_missing(monkeypatch):
    import psycopg

    class Result:
        def fetchall(self):
            return [("rfq_quotes",), ("line_items",)]

    class FakeConn:
        def execute(self, sql, params=None):
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConn())
    sink = PostgresSink("postgresql://local/test")

    assert sink.verify_quote_tables() == {"compliance"}
