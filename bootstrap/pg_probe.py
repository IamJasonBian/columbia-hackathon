#!/usr/bin/env python3
"""Quick DATABASE_URL connectivity + optional probe write."""

from __future__ import annotations

import argparse
import os
import sys

from extraction_service.db.postgres import PostgresSink, database_url_from_env
from extraction_service.models import DocumentKind, ExtractedDocument


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Insert a probe row")
    parser.add_argument("--url", default=None, help="Override DATABASE_URL")
    args = parser.parse_args()

    url = args.url or database_url_from_env()
    if not url:
        raise SystemExit("Set DATABASE_URL or pass --url")

    sink = PostgresSink(url)
    db = sink.ping()
    print(f"connected: {db}")

    if args.write:
        doc = ExtractedDocument(
            filename="probe.eml",
            kind=DocumentKind.email,
            metadata={"subject": "pg_probe", "from": "probe@local"},
            text="ok",
        )
        sink.write("_probe", "pg_probe", doc)
        print("wrote mailbox_messages row gmail_message_id=pg_probe")


if __name__ == "__main__":
    main()
