#!/usr/bin/env python3
"""Enqueue or process pull JSON → rfq_quotes + line_items."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from extraction_service.db.postgres import PostgresSink, database_url_from_env
from extraction_service.db.sqlite_queue import ServiceQueue
from extraction_service.models import ExtractedDocument
from extraction_service.quote.poller_ingest import persist_extracted_document
from extraction_service.quote.repository import QuoteRepository
from extraction_service.quote.task_enqueue import enqueue_rfq_line_items_task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mailbox", default="jason.bian57@gmail.com")
    parser.add_argument(
        "--pulls-dir",
        type=Path,
        default=Path("app_storage/pulls/jason.bian57@gmail.com"),
    )
    parser.add_argument("--message-id", action="append", dest="message_ids")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument(
        "--enqueue-only",
        action="store_true",
        help="Only add SQLite tasks (default without --process-now)",
    )
    parser.add_argument(
        "--process-now",
        action="store_true",
        help="Write Postgres immediately (bypass worker; for backfill)",
    )
    args = parser.parse_args()

    queue = ServiceQueue()
    paths: list[Path]
    if args.message_ids:
        paths = [args.pulls_dir / f"{mid}.json" for mid in args.message_ids]
    else:
        paths = sorted(
            p for p in args.pulls_dir.glob("*.json") if p.name != "_state.json"
        )
        paths.reverse()
        if args.limit:
            paths = paths[: args.limit]

    repo = None
    if args.process_now:
        url = database_url_from_env()
        if not url:
            raise SystemExit("Set DATABASE_URL for --process-now")
        repo = QuoteRepository(PostgresSink(url))

    for path in paths:
        if not path.exists():
            print(f"skip missing {path.name}")
            continue
        doc = ExtractedDocument.model_validate(json.loads(path.read_text()))
        mid = path.stem
        if args.process_now:
            assert repo is not None
            qid = persist_extracted_document(
                mid, doc, mailbox=args.mailbox, repo=repo, queue=queue
            )
            print(f"{mid} -> quote_id={qid} (direct)")
        else:
            job_id, task_id = enqueue_rfq_line_items_task(
                queue,
                mailbox=args.mailbox,
                gmail_message_id=mid,
                doc=doc,
                source="ingest_script",
            )
            print(f"{mid} -> task_id={task_id} job_id={job_id} (queued)")

    print(f"done files={len(paths)}")


if __name__ == "__main__":
    main()
