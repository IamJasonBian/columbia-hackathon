#!/usr/bin/env python3
"""Backfill PostgreSQL from on-disk pull JSON (app_storage/pulls)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from extraction_service.db.postgres import PostgresSink, database_url_from_env
from extraction_service.models import ExtractedDocument


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pulls", default="app_storage/pulls", help="Root pulls directory")
    parser.add_argument("--mailbox", help="Only sync this mailbox folder")
    args = parser.parse_args()

    url = database_url_from_env()
    if not url:
        raise SystemExit("Set DATABASE_URL (or POSTGRES_URL)")

    sink = PostgresSink(url)
    sink.ensure_schema()
    root = Path(args.pulls)
    mailboxes = [root / args.mailbox] if args.mailbox else sorted(p for p in root.iterdir() if p.is_dir())

    count = 0
    for mbox_dir in mailboxes:
        mailbox = mbox_dir.name
        state_path = mbox_dir / "_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            sink.save_poll_state(mailbox, int(state["after_epoch"]))
        for path in sorted(mbox_dir.glob("*.json")):
            if path.name == "_state.json":
                continue
            doc = ExtractedDocument(**json.loads(path.read_text()))
            sink.write(mailbox, path.stem, doc)
            count += 1
            print(f"synced {mailbox}/{path.stem}", flush=True)
    print(f"done: {count} message(s) → {sink.ping()}", flush=True)


if __name__ == "__main__":
    main()
