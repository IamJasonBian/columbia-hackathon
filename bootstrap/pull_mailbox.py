#!/usr/bin/env python3
"""Pull a connected mailbox's messages and run them through extraction_service.

Usage:
    python bootstrap/pull_mailbox.py jason.bian57@gmail.com --query 'RFQ has:attachment'
    EMAIL_OAUTH_STORE=firestore python bootstrap/pull_mailbox.py <mailbox> --out app_storage/pulls

Each message is fetched raw (RFC 822), extracted, and written to PostgreSQL.
Use --file-store for legacy local JSON output.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from extraction_service.extractors import extract_document
from extraction_service.gmail.client import DEFAULT_QUERY, GmailClient
from extraction_service.gmail.creds_store import default_store
from extraction_service.gmail.oauth import access_token
from extraction_service.models import ExtractedDocument


def _summary(message_id: str, doc: ExtractedDocument) -> str:
    atts = ", ".join(
        f"{a.filename} [{a.kind.value}, {len(a.pages) or len(a.tables)} pg/tbl, {len(a.text)} chars]"
        for a in doc.attachments
    ) or "no attachments"
    return f"{message_id} | {doc.metadata.get('date')} | {doc.metadata.get('from')} | {doc.metadata.get('subject')!r} | {atts}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mailbox")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--max", type=int, default=25)
    parser.add_argument("--out", default="app_storage/pulls")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--file-store", action="store_true")
    args = parser.parse_args()

    creds = default_store().get(args.mailbox)
    if creds is None:
        raise SystemExit(f"{args.mailbox} is not connected — run /gmail-oauth/start?email={args.mailbox}")
    client = GmailClient(access_token(creds))

    ids = client.search(args.query, args.max)
    print(f"{len(ids)} message(s) for {args.query!r}")
    if args.file_store:
        out_dir = Path(args.out) / args.mailbox
        out_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(out_dir, 0o700)
        sink = None
        destination = str(out_dir)
    else:
        from extraction_service.db.postgres import PostgresSink, database_url_from_env

        db_url = database_url_from_env()
        if not db_url:
            raise SystemExit("DATABASE_URL (or POSTGRES_URL) is required; use --file-store for local JSON")
        sink = PostgresSink(db_url)
        sink.ensure_schema()
        destination = f"postgres:{sink.ping()}"
    for message_id in ids:
        raw = client.get_raw(message_id)
        doc = extract_document(raw, f"{message_id}.eml", "message/rfc822", enable_ocr=not args.no_ocr)
        if sink:
            sink.write(creds.email, message_id, doc)
        else:
            (out_dir / f"{message_id}.json").write_text(
                json.dumps(doc.model_dump(mode="json"), indent=2)
            )
        print(_summary(message_id, doc))
    print(f"wrote {len(ids)} message(s) to {destination}")


if __name__ == "__main__":
    main()
