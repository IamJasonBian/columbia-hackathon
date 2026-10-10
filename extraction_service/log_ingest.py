"""Parse and persist service log files."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class LogEntry:
    service: str
    source: str
    source_offset: int
    occurred_at: datetime | None
    level: str
    message: str
    request_method: str | None = None
    request_path: str | None = None
    status_code: int | None = None
    client_ip: str | None = None


_UVICORN_ACCESS_RE = re.compile(
    r'^(?P<client>.+):\d+ - "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/\d(?:\.\d)?" '
    r"(?P<status>\d{3})(?: .*)?$"
)
_CLOUDFLARED_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d\d-\d\dT\S+Z) (?P<level>DBG|INF|WRN|ERR) (?P<message>.*)$"
)
_PYTHON_LOG_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)?) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<message>.*)$"
)
_LEVELS = {"DBG": "DEBUG", "INF": "INFO", "WRN": "WARNING", "ERR": "ERROR"}
_SECRET_QUERY_KEYS = {"token", "access_token", "api_key", "key", "secret"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_logs (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    service TEXT NOT NULL,
    source TEXT NOT NULL,
    source_offset BIGINT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    request_method TEXT,
    request_path TEXT,
    status_code INT,
    client_ip TEXT,
    UNIQUE (source, source_offset)
);

CREATE INDEX IF NOT EXISTS idx_service_logs_service_id
    ON service_logs (service, id);
CREATE INDEX IF NOT EXISTS idx_service_logs_occurred_at
    ON service_logs (occurred_at DESC);
"""


def _redact_url(raw: str) -> str:
    parts = urlsplit(raw)
    if not parts.query:
        return raw
    query = [
        (key, "[REDACTED]" if key.lower() in _SECRET_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, safe="[]"), parts.fragment))


def _redact_text(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)\S+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret)=([^\s&]+)",
        lambda match: f"{match.group(1)}=[REDACTED]",
        value,
    )
    return value


def parse_log_line(service: str, source: str, source_offset: int, line: str) -> LogEntry:
    raw = line.rstrip("\r\n")
    occurred_at = None
    level = "INFO"
    message = raw

    cloudflared = _CLOUDFLARED_RE.match(raw)
    python_log = _PYTHON_LOG_RE.match(raw)
    if cloudflared:
        occurred_at = datetime.fromisoformat(cloudflared.group("timestamp").replace("Z", "+00:00"))
        level = _LEVELS[cloudflared.group("level")]
        message = cloudflared.group("message")
    elif python_log:
        occurred_at = datetime.fromisoformat(
            python_log.group("timestamp").replace(",", ".")
        )
        level = python_log.group("level")
        message = python_log.group("message")
    else:
        uvicorn_level = re.match(
            r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+(.*)$", raw
        )
        if uvicorn_level:
            level = uvicorn_level.group(1)
            message = uvicorn_level.group(2)

    request_method = None
    request_path = None
    status_code = None
    client_ip = None
    access = _UVICORN_ACCESS_RE.match(message)
    if access:
        request_method = access.group("method")
        request_path = _redact_url(access.group("path"))
        status_code = int(access.group("status"))
        client_ip = access.group("client")
        message = message.replace(access.group("path"), request_path, 1)

    return LogEntry(
        service=service,
        source=source,
        source_offset=source_offset,
        occurred_at=occurred_at,
        level=level,
        message=_redact_text(message),
        request_method=request_method,
        request_path=request_path,
        status_code=status_code,
        client_ip=client_ip,
    )


def iter_log_file(
    path: Path, service: str, *, source: str | None = None
) -> Iterator[LogEntry]:
    source_name = source or str(path)
    with path.open("rb") as handle:
        while line := handle.readline():
            offset = handle.tell() - len(line)
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.strip():
                yield parse_log_line(service, source_name, offset, text)


class PostgresLogStore:
    def __init__(self, conninfo: str, *, connect_timeout: float = 15):
        self.conninfo = conninfo
        self.connect_timeout = connect_timeout
        self._schema_ready = False

    @classmethod
    def from_env(cls) -> PostgresLogStore:
        project_env = Path(__file__).resolve().parents[1] / ".env"
        load_dotenv(project_env)
        conninfo = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
        if not conninfo:
            raise RuntimeError("DATABASE_URL (or POSTGRES_URL) is required")
        return cls(conninfo)

    def _connect(self):
        import psycopg

        return psycopg.connect(self.conninfo, connect_timeout=self.connect_timeout)

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()
        self._schema_ready = True

    def write_many(self, entries: Iterable[LogEntry]) -> int:
        rows = list(entries)
        if not rows:
            return 0
        self.ensure_schema()
        sql = """
            INSERT INTO service_logs (
                occurred_at, service, source, source_offset, level, message,
                request_method, request_path, status_code, client_ip
            )
            VALUES (
                COALESCE(%s, now()), %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source, source_offset) DO NOTHING
        """
        with self._connect() as conn:
            for entry in rows:
                conn.execute(
                    sql,
                    (
                        entry.occurred_at,
                        entry.service,
                        entry.source,
                        entry.source_offset,
                        entry.level,
                        entry.message,
                        entry.request_method,
                        entry.request_path,
                        entry.status_code,
                        entry.client_ip,
                    ),
                )
            conn.commit()
        return len(rows)

    def list(
        self, *, service: str | None = None, after_id: int = 0, limit: int = 100
    ) -> list[dict[str, object]]:
        self.ensure_schema()
        conditions = ["id > %s"]
        params: list[object] = [after_id]
        if service:
            conditions.append("service = %s")
            params.append(service)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, occurred_at, service, level, message, request_method,
                       request_path, status_code, client_ip
                FROM service_logs
                WHERE {" AND ".join(conditions)}
                ORDER BY id
                LIMIT %s
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "occurred_at": row[1].isoformat(),
                "service": row[2],
                "level": row[3],
                "message": row[4],
                "request_method": row[5],
                "request_path": row[6],
                "status_code": row[7],
                "client_ip": row[8],
            }
            for row in rows
        ]


def ingest_paths(
    store: PostgresLogStore,
    paths: dict[str, Path],
    *,
    source_root: Path | None = None,
) -> int:
    total = 0
    for service, path in paths.items():
        if not path.is_file():
            continue
        try:
            source = str(path.relative_to(source_root)) if source_root else str(path)
        except ValueError:
            source = str(path)
        total += store.write_many(iter_log_file(path, service, source=source))
    return total
