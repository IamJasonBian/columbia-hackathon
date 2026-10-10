#!/usr/bin/env python3
"""Poll a connected mailbox every N seconds and extract new document mail.

Usage:
    EMAIL_OAUTH_STORE=firestore python bootstrap/poll_mailbox.py jason.bian57@gmail.com
    python bootstrap/poll_mailbox.py <mailbox> --interval 10 --lookback-hours 24 --out app_storage/pulls

Writes extracted messages and poll state to PostgreSQL, then prints a line per
new message. Use --file-store only for an offline local run.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path

from extraction_service.gmail.creds_store import default_store
from extraction_service.gmail.poller import (
    DOCUMENT_QUERY,
    FileMailboxStore,
    MailboxPoller,
    PostgresMailboxStore,
)
from extraction_service.models import ExtractedDocument


def _print(message_id: str, doc: ExtractedDocument) -> None:
    atts = ", ".join(
        f"{a.filename} [{a.kind.value}, {len(a.text)} chars{', ' + str(len(a.warnings)) + ' warn' if a.warnings else ''}]"
        for a in doc.attachments
    )
    print(f"NEW {message_id} | {doc.metadata.get('date')} | {doc.metadata.get('from')} | "
          f"{doc.metadata.get('subject')!r} | {atts}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mailbox")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--query", default=DOCUMENT_QUERY)
    parser.add_argument("--out", default="app_storage/pulls")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument(
        "--file-store",
        action="store_true",
        help="Use legacy local JSON persistence instead of PostgreSQL",
    )
    parser.add_argument(
        "--no-line-items",
        action="store_true",
        help="Skip enqueueing rfq_line_items tasks in SQLite",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    creds = default_store().get(args.mailbox)
    if creds is None:
        raise SystemExit(f"{args.mailbox} is not connected — run /gmail-oauth/start?email={args.mailbox}")

    on_document = _print
    on_extracted = None
    if args.file_store:
        out_dir = Path(args.out) / args.mailbox
        store = FileMailboxStore(out_dir)
        os.chmod(out_dir, 0o700)
        destination = str(out_dir)
    else:
        from extraction_service.db.postgres import PostgresSink, database_url_from_env

        db_url = database_url_from_env()
        if not db_url:
            raise SystemExit("DATABASE_URL (or POSTGRES_URL) is required; use --file-store for local JSON")
        sink = PostgresSink(db_url)
        sink.ensure_schema()
        store = PostgresMailboxStore(sink, creds.email)
        destination = f"postgres:{sink.ping()}"

    if not args.no_line_items:
        from extraction_service.quote.task_enqueue import RfqTaskEnqueuer

        enqueuer = RfqTaskEnqueuer.for_mailbox(creds.email)

        def _enqueue(message_id: str, doc: ExtractedDocument) -> None:
            task_id = enqueuer(message_id, doc)
            print(f"TASK {task_id} queued for {message_id}", flush=True)

        on_extracted = _enqueue
        destination += " + sqlite tasks (worker → Postgres)"

    poller = MailboxPoller(
        creds,
        store=store,
        query=args.query,
        lookback_seconds=int(args.lookback_hours * 3600),
        enable_ocr=not args.no_ocr,
        on_extracted=on_extracted,
        on_document=on_document,
    )

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(f"polling {args.mailbox} every {args.interval}s → {destination}", flush=True)
    poller.run(args.interval, stop=lambda: stopping)
    print("stopped", flush=True)


if __name__ == "__main__":
    main()
