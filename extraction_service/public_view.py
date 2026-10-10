"""Read-only, token-gated view of poller output — safe to expose publicly.

Serves ONLY poller state and per-message summaries (date, sender, subject,
attachment names). Full extraction text and the upload/extract endpoints stay
on the local-only service (extraction_service.main).

Auth: PUBLIC_VIEW_TOKEN, sent as ``?token=...`` or ``Authorization: Bearer``.
The app refuses to start without it.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from extraction_service.log_ingest import PostgresLogStore
from extraction_service.main import list_polled_mailboxes, poll_messages, poll_state


def _require_token(request: Request) -> None:
    expected = os.environ.get("PUBLIC_VIEW_TOKEN", "")
    supplied = request.query_params.get("token") or ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "missing or invalid token")


app = FastAPI(
    title="Mailbox Poller (read-only)",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(_require_token)],
)


@app.get("/")
def index() -> dict[str, Any]:
    return {
        "mailboxes": list_polled_mailboxes(),
        "routes": ["/pulls/{mailbox}/state", "/pulls/{mailbox}/messages", "/logs"],
    }


@app.get("/pulls/{mailbox}/state")
def state(mailbox: str) -> dict[str, Any]:
    return poll_state(mailbox)


@app.get("/pulls/{mailbox}/messages")
def messages(mailbox: str) -> list[dict[str, Any]]:
    return poll_messages(mailbox, 100)


@lru_cache(maxsize=1)
def _log_store() -> PostgresLogStore:
    try:
        return PostgresLogStore.from_env()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/logs")
def logs(
    service: str | None = None,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[dict[str, object]]:
    """Return redacted PostgreSQL-backed service logs in insertion order."""
    return _log_store().list(service=service, after_id=after_id, limit=limit)


def run() -> None:
    import uvicorn

    if not os.environ.get("PUBLIC_VIEW_TOKEN"):
        raise SystemExit("Set PUBLIC_VIEW_TOKEN before exposing the public view")
    port = int(os.environ.get("PUBLIC_VIEW_PORT", "8013"))
    uvicorn.run("extraction_service.public_view:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    run()
