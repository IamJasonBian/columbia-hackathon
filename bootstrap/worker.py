#!/usr/bin/env python3
"""SQLite pipeline worker — hot-load latest code on each restart.

Run separately from the poller (and from the HTTP server). Restart this
process after deploy so it picks up new Python code; each job re-reads the
latest pull JSON from disk before writing Postgres.

Usage:
    export DATABASE_URL=...
    python bootstrap/worker.py
"""

from __future__ import annotations

import logging
import signal

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("worker")


def main() -> None:
    from extraction_service.quote.pipeline_worker import PipelineWorker

    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    worker = PipelineWorker.from_env()
    logger.info("pipeline worker started (SQLite → hot-load → Postgres)")
    worker.run_forever(stop=lambda: stop)
    logger.info("pipeline worker stopped")


if __name__ == "__main__":
    main()
