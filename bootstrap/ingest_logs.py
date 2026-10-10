#!/usr/bin/env python3
"""Import local service logs into PostgreSQL.

Re-running is safe: each source file and byte offset is inserted only once.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from extraction_service.log_ingest import PostgresLogStore, ingest_paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storage-dir",
        type=Path,
        default=Path("app_storage"),
        help="Directory containing service .log files",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    storage = args.storage_dir if args.storage_dir.is_absolute() else root / args.storage_dir
    paths = {
        "extraction_service": storage / "extraction_service.log",
        "gmail_poller": storage / "poll_jason.bian57.log",
        "public_view": storage / "public_view.log",
        "cloudflared": storage / "cloudflared.log",
    }
    count = ingest_paths(PostgresLogStore.from_env(), paths, source_root=root)
    print(f"Submitted {count} log rows to PostgreSQL")


if __name__ == "__main__":
    main()
