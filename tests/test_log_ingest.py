from __future__ import annotations

from datetime import datetime, timezone

from extraction_service.log_ingest import (
    LogEntry,
    PostgresLogStore,
    ingest_paths,
    iter_log_file,
    parse_log_line,
)


def test_parse_uvicorn_access_log_redacts_query_tokens():
    entry = parse_log_line(
        "public_view",
        "app_storage/public_view.log",
        42,
        'INFO:     127.0.0.1:1234 - "GET /pulls/a/messages?token=secret HTTP/1.1" 200 OK',
    )

    assert entry == LogEntry(
        service="public_view",
        source="app_storage/public_view.log",
        source_offset=42,
        occurred_at=None,
        level="INFO",
        message='127.0.0.1:1234 - "GET /pulls/a/messages?token=[REDACTED] HTTP/1.1" 200 OK',
        request_method="GET",
        request_path="/pulls/a/messages?token=[REDACTED]",
        status_code=200,
        client_ip="127.0.0.1",
    )


def test_parse_cloudflared_log_extracts_timestamp_and_level():
    entry = parse_log_line(
        "cloudflared",
        "app_storage/cloudflared.log",
        0,
        "2026-10-10T16:35:12Z INF Registered tunnel connection connIndex=0",
    )

    assert entry.occurred_at == datetime(2026, 10, 10, 16, 35, 12, tzinfo=timezone.utc)
    assert entry.level == "INFO"
    assert entry.message == "Registered tunnel connection connIndex=0"


def test_parse_plain_log_redacts_bearer_and_key_value_secrets():
    entry = parse_log_line(
        "worker",
        "app_storage/worker.log",
        7,
        "2026-10-10 12:00:00,123 ERROR Authorization: Bearer abc API_KEY=def failed",
    )

    assert entry.level == "ERROR"
    assert "abc" not in entry.message
    assert "def" not in entry.message
    assert entry.source_offset == 7


def test_iter_log_file_uses_stable_byte_offsets(tmp_path):
    path = tmp_path / "service.log"
    path.write_text("first\n\nsecond\n", encoding="utf-8")

    entries = list(iter_log_file(path, "core", source="app_storage/service.log"))

    assert [(entry.source_offset, entry.message) for entry in entries] == [
        (0, "first"),
        (7, "second"),
    ]


def test_postgres_store_inserts_idempotently_and_lists_logs(monkeypatch):
    import psycopg

    executed: list[tuple[str, tuple | None]] = []

    class Result:
        def __init__(self, rows=()):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class FakeConn:
        def execute(self, sql, params=None):
            executed.append((sql, params))
            if sql.lstrip().startswith("SELECT"):
                return Result(
                    [
                        (
                            3,
                            datetime(2026, 10, 10, tzinfo=timezone.utc),
                            "core",
                            "INFO",
                            "ready",
                            None,
                            None,
                            None,
                            None,
                        )
                    ]
                )
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def commit(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConn())
    store = PostgresLogStore("postgresql://local/test")
    store._schema_ready = True
    entry = LogEntry("core", "core.log", 0, None, "INFO", "ready")

    assert store.write_many([entry]) == 1
    assert "ON CONFLICT (source, source_offset) DO NOTHING" in executed[0][0]
    assert store.list(service="core", after_id=1, limit=10) == [
        {
            "id": 3,
            "occurred_at": "2026-10-10T00:00:00+00:00",
            "service": "core",
            "level": "INFO",
            "message": "ready",
            "request_method": None,
            "request_path": None,
            "status_code": None,
            "client_ip": None,
        }
    ]


def test_ingest_paths_skips_missing_files(tmp_path):
    existing = tmp_path / "core.log"
    existing.write_text("ready\n", encoding="utf-8")

    class FakeStore:
        def write_many(self, entries):
            rows = list(entries)
            assert [(row.service, row.message) for row in rows] == [("core", "ready")]
            return len(rows)

    count = ingest_paths(
        FakeStore(),
        {
            "core": existing,
            "missing": tmp_path / "missing.log",
        },
        source_root=tmp_path,
    )

    assert count == 1
