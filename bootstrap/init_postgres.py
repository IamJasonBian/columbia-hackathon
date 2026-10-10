#!/usr/bin/env python3
"""Create and verify the application's PostgreSQL tables."""

from __future__ import annotations

from services.extraction.db.postgres import PostgresSink, database_url_from_env


def main() -> None:
    url = database_url_from_env()
    if not url:
        raise SystemExit("DATABASE_URL (or POSTGRES_URL) is required")

    sink = PostgresSink(url)
    sink.ensure_schema()
    missing = sink.verify_quote_tables()
    if missing:
        raise SystemExit(f"PostgreSQL schema incomplete; missing: {', '.join(sorted(missing))}")

    print("PostgreSQL schema ready: rfq_quotes, doc_lines.lines, compliance")


if __name__ == "__main__":
    main()
