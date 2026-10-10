"""Persistence for extracted mailbox documents."""

from extraction_service.db.postgres import PostgresSink, database_url_from_env, iter_table_records
from extraction_service.db.sqlite_queue import ServiceQueue, default_queue_path

__all__ = [
    "PostgresSink",
    "ServiceQueue",
    "database_url_from_env",
    "default_queue_path",
    "iter_table_records",
]
