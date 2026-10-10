from __future__ import annotations

from extraction_service.db.postgres import PostgresSink
from extraction_service.quote.models import ComplianceRecord, LineItemRecord, RfqQuoteHeader
from extraction_service.quote.repository import QuoteRepository


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
            LineItemRecord(
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


def test_customer_and_compliance_source_are_modeled():
    header = RfqQuoteHeader(customer_id="customer-1")
    record = ComplianceRecord(
        requirement_code="ein",
        requirement_label="EIN Letter",
        source="customer_mapping",
    )

    assert header.customer_id == "customer-1"
    assert record.source == "customer_mapping"


def test_replace_compliance_source_only_replaces_that_source(monkeypatch):
    calls: list[tuple[str, object]] = []
    inserted: list[tuple] = []

    class FakeTx:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    class FakeCur:
        def executemany(self, sql, params):
            inserted.extend(params)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return None

        def cursor(self):
            return FakeCur()

        def transaction(self):
            return FakeTx()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConn())
    repo = QuoteRepository(PostgresSink("postgresql://local/test"))
    repo.sink._schema_ready = True
    repo.replace_compliance_source(
        99,
        "customer_mapping",
        [
            ComplianceRecord(
                line_item_id=11,
                requirement_code="ein",
                requirement_label="EIN Letter",
                source="customer_mapping",
                status="satisfied",
                attachment_path="ein.pdf",
            )
        ],
    )

    delete_sql, delete_params = calls[0]
    assert "source = %s" in delete_sql
    assert delete_params == (99, "customer_mapping")
    assert inserted[0][1:5] == (11, "ein", "EIN Letter", "customer_mapping")
