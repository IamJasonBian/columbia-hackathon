"""Review service: browse RFQ quotes and edit line_items under a per-row lease.

Clicking a row in the UI acquires a lease in ``line_item_locks``. Only the
holder may PATCH that row; the lease expires after LOCK_TTL_SECONDS unless the
client heartbeats it. A user holds at most one lease at a time — taking a new
row releases the previous one.

We deliberately do not hold a Postgres ``SELECT ... FOR UPDATE`` across HTTP
requests (that would pin a connection/transaction to a browser tab); the
lease row is the lock, and every write re-checks it inside a transaction.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import ForeignKeyViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

LOCK_TTL_SECONDS = int(os.environ.get("LOCK_TTL_SECONDS", "45"))
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "7"))
SESSION_COOKIE = "review_session"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
# Lets you sign in with any email, no Google. Local testing only.
DEV_AUTH = os.environ.get("REVIEW_DEV_AUTH", "").lower() in ("1", "true", "yes")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
FRONTEND_DIST = ROOT / "frontend" / "dist"
# Comma-separated. Both empty = anyone with a verified Google account may sign in.
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
ALLOWED_DOMAINS = {
    d.strip().lower().lstrip("@") for d in os.environ.get("ALLOWED_DOMAINS", "").split(",") if d.strip()
}

pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global pool
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=int(os.environ.get("REVIEW_DB_POOL_SIZE", "10")),
        # Autocommit: single statements commit immediately; multi-step writes use
        # conn.transaction(). Avoids commits landing after the response is sent.
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
    )
    with pool.connection() as conn:
        conn.execute((Path(__file__).parent / "schema.sql").read_text())
    yield
    pool.close()


app = FastAPI(title="Line Item Review", version="0.1.0", lifespan=lifespan)


def db():
    assert pool is not None
    with pool.connection() as conn:
        yield conn


# --------------------------------------------------------------------------- auth


class User(BaseModel):
    id: int
    email: str
    name: str | None = None
    picture: str | None = None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _start_session(conn: Any, response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO app_sessions (token_hash, user_id, expires_at)
        VALUES (%s, %s, now() + make_interval(days => %s))
        """,
        (_hash(token), user_id, SESSION_DAYS),
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )


def _is_allowed(email: str) -> bool:
    if not ALLOWED_EMAILS and not ALLOWED_DOMAINS:
        return True
    email = email.lower()
    return email in ALLOWED_EMAILS or email.rsplit("@", 1)[-1] in ALLOWED_DOMAINS


def _require_allowed(email: str) -> None:
    if not _is_allowed(email):
        raise HTTPException(403, f"{email} is not on the access list")


def _upsert_user(conn: Any, sub: str, email: str, name: str | None, picture: str | None) -> User:
    row = conn.execute(
        """
        INSERT INTO app_users (google_sub, email, name, picture)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (google_sub) DO UPDATE SET
            email = EXCLUDED.email,
            name = EXCLUDED.name,
            picture = EXCLUDED.picture,
            last_login_at = now()
        RETURNING id, email, name, picture
        """,
        (sub, email, name, picture),
    ).fetchone()
    return User(**row)


def current_user(
    conn: Any = Depends(db),
    review_session: str | None = Cookie(default=None),
) -> User:
    if not review_session:
        raise HTTPException(401, "not signed in")
    row = conn.execute(
        """
        SELECT u.id, u.email, u.name, u.picture
        FROM app_sessions s JOIN app_users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > now()
        """,
        (_hash(review_session),),
    ).fetchone()
    if row is None:
        raise HTTPException(401, "session expired")
    # Re-check so removing someone from the allowlist cuts off live sessions too.
    _require_allowed(row["email"])
    return User(**row)


class GoogleLogin(BaseModel):
    credential: str


class DevLogin(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    name: str | None = None


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "google_client_id": GOOGLE_CLIENT_ID or None,
        "dev_auth": DEV_AUTH,
        "lock_ttl_seconds": LOCK_TTL_SECONDS,
    }


@app.post("/api/auth/google")
def login_google(body: GoogleLogin, response: Response, conn: Any = Depends(db)) -> User:
    """Verify a Google Identity Services ID token; first login creates the account."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "GOOGLE_CLIENT_ID is not configured")
    try:
        claims = id_token.verify_oauth2_token(
            body.credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError as exc:
        raise HTTPException(401, f"invalid Google credential: {exc}") from exc
    if not claims.get("email_verified"):
        raise HTTPException(403, "Google account email is not verified")
    _require_allowed(claims["email"])
    user = _upsert_user(
        conn, claims["sub"], claims["email"], claims.get("name"), claims.get("picture")
    )
    _start_session(conn, response, user.id)
    return user


@app.post("/api/auth/dev")
def login_dev(body: DevLogin, response: Response, conn: Any = Depends(db)) -> User:
    if not DEV_AUTH:
        raise HTTPException(404)
    _require_allowed(body.email)
    user = _upsert_user(conn, f"dev:{body.email}", body.email, body.name or body.email, None)
    _start_session(conn, response, user.id)
    return user


@app.post("/api/auth/logout")
def logout(
    response: Response,
    conn: Any = Depends(db),
    review_session: str | None = Cookie(default=None),
) -> dict[str, bool]:
    if review_session:
        row = conn.execute(
            "DELETE FROM app_sessions WHERE token_hash = %s RETURNING user_id",
            (_hash(review_session),),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM line_item_locks WHERE user_id = %s", (row["user_id"],))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> User:
    return user


# --------------------------------------------------------------------------- reads

_LINE_COLUMNS = """
    l.id, l.quote_id, l.line_seq, l.line_no, l.rfq_format, l.buyer_part_no,
    l.description, l.quantity, l.unit_of_measure, l.unit_price, l.delivery,
    l.is_option, l.required_qty_raw, l.source_attachment, l.source_page,
    l.source_table, l.match_score, l.quoted_unit_price, l.extended_price,
    l.market_benchmark_price, l.price_rationale, l.line_status,
    l.clarifying_question, l.availability_note, l.created_at
"""

_LOCK_COLUMNS = """
    CASE WHEN k.expires_at > now() THEN json_build_object(
        'user_id', k.user_id,
        'email', u.email,
        'name', u.name,
        'picture', u.picture,
        'acquired_at', k.acquired_at,
        'expires_at', k.expires_at
    ) END AS lock
"""


_COMPLIANCE_COLUMNS = """
    id, quote_id, line_item_id, requirement_code, requirement_label, source,
    matched_pattern, source_text, status, required_document_type,
    attachment_path, buyer_message, created_at
"""


def _fetch_line(conn: Any, line_item_id: int) -> dict[str, Any] | None:
    return conn.execute(
        f"""
        SELECT {_LINE_COLUMNS}, {_LOCK_COLUMNS}
        FROM line_items l
        LEFT JOIN line_item_locks k ON k.line_item_id = l.id
        LEFT JOIN app_users u ON u.id = k.user_id
        WHERE l.id = %s
        """,
        (line_item_id,),
    ).fetchone()


@app.get("/api/quotes")
def list_quotes(
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    _user: User = Depends(current_user),
    conn: Any = Depends(db),
) -> list[dict[str, Any]]:
    pattern = f"%{q}%" if q else None
    return conn.execute(
        """
        SELECT r.id, r.rfq_number, r.email_subject, r.buyer_organization,
               r.workflow_status, r.source_message_date,
               count(l.id) AS line_count,
               count(k.line_item_id) FILTER (WHERE k.expires_at > now()) AS locked_count,
               (SELECT count(*) FROM compliance c
                WHERE c.quote_id = r.id AND c.status IN ('required', 'document_missing')
               ) AS open_compliance_count
        FROM rfq_quotes r
        JOIN line_items l ON l.quote_id = r.id
        LEFT JOIN line_item_locks k ON k.line_item_id = l.id
        WHERE %(p)s::text IS NULL
           OR r.rfq_number ILIKE %(p)s
           OR r.email_subject ILIKE %(p)s
           OR r.buyer_organization ILIKE %(p)s
        GROUP BY r.id
        ORDER BY r.source_message_date DESC NULLS LAST, r.id DESC
        LIMIT %(limit)s
        """,
        {"p": pattern, "limit": limit},
    ).fetchall()


@app.get("/api/quotes/{quote_id}")
def get_quote(
    quote_id: int,
    _user: User = Depends(current_user),
    conn: Any = Depends(db),
) -> dict[str, Any]:
    quote = conn.execute(
        """
        SELECT id, rfq_number, email_subject, buyer_organization,
               workflow_status, source_message_date, mailbox
        FROM rfq_quotes WHERE id = %s
        """,
        (quote_id,),
    ).fetchone()
    if quote is None:
        raise HTTPException(404, "quote not found")
    lines = conn.execute(
        f"""
        SELECT {_LINE_COLUMNS}, {_LOCK_COLUMNS}
        FROM line_items l
        LEFT JOIN line_item_locks k ON k.line_item_id = l.id
        LEFT JOIN app_users u ON u.id = k.user_id
        WHERE l.quote_id = %s
        ORDER BY l.line_seq
        """,
        (quote_id,),
    ).fetchall()
    compliance = conn.execute(
        f"SELECT {_COMPLIANCE_COLUMNS} FROM compliance WHERE quote_id = %s ORDER BY id",
        (quote_id,),
    ).fetchall()
    return {
        **quote,
        "line_items": lines,
        "compliance": compliance,
        "server_time": datetime.now().astimezone(),
    }


# --------------------------------------------------------------------------- locks


def _lock_conflict(conn: Any, line_item_id: int) -> HTTPException:
    holder = conn.execute(
        """
        SELECT k.user_id, u.email, u.name, k.expires_at
        FROM line_item_locks k JOIN app_users u ON u.id = k.user_id
        WHERE k.line_item_id = %s
        """,
        (line_item_id,),
    ).fetchone()
    return HTTPException(
        409, jsonable_encoder({"message": "row is locked by another user", "lock": holder})
    )


@app.post("/api/line-items/{line_item_id}/lock")
def acquire_lock(
    line_item_id: int,
    user: User = Depends(current_user),
    conn: Any = Depends(db),
) -> dict[str, Any]:
    """Take or renew the lease on one row; releases any other row this user holds.

    Also serves as the heartbeat: call it again before expires_at to extend.

    Raises:
        HTTPException: 404 if the row is gone, 409 if someone else holds a live lease.
    """
    try:
        row = _try_acquire(conn, line_item_id, user.id)
    except ForeignKeyViolation as exc:  # pipeline replaced the quote's lines
        raise HTTPException(404, "line item not found") from exc
    if row is None:
        raise _lock_conflict(conn, line_item_id)
    return {**row, "ttl_seconds": LOCK_TTL_SECONDS}


def _try_acquire(conn: Any, line_item_id: int, user_id: int) -> dict[str, Any] | None:
    with conn.transaction():
        conn.execute(
            "DELETE FROM line_item_locks WHERE user_id = %s AND line_item_id <> %s",
            (user_id, line_item_id),
        )
        # Upsert wins only if the row is unlocked, already ours, or the lease lapsed.
        row = conn.execute(
            """
            INSERT INTO line_item_locks (line_item_id, user_id, expires_at)
            VALUES (%s, %s, now() + make_interval(secs => %s))
            ON CONFLICT (line_item_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                acquired_at = CASE
                    WHEN line_item_locks.user_id = EXCLUDED.user_id
                     AND line_item_locks.expires_at > now()
                    THEN line_item_locks.acquired_at ELSE now() END,
                expires_at = EXCLUDED.expires_at
            WHERE line_item_locks.user_id = EXCLUDED.user_id
               OR line_item_locks.expires_at <= now()
            RETURNING line_item_id, user_id, acquired_at, expires_at
            """,
            (line_item_id, user_id, LOCK_TTL_SECONDS),
        ).fetchone()
    return row


@app.delete("/api/line-items/{line_item_id}/lock")
@app.post("/api/line-items/{line_item_id}/unlock")  # sendBeacon on tab close can only POST
def release_lock(
    line_item_id: int,
    user: User = Depends(current_user),
    conn: Any = Depends(db),
) -> dict[str, bool]:
    row = conn.execute(
        "DELETE FROM line_item_locks WHERE line_item_id = %s AND user_id = %s RETURNING 1",
        (line_item_id, user.id),
    ).fetchone()
    return {"released": row is not None}


# --------------------------------------------------------------------------- writes

LineStatus = Literal["pending", "parsed", "matched", "priced", "ambiguous", "unmatched", "rejected"]


class LineItemPatch(BaseModel):
    """Editable line_items columns. Omitted fields are left untouched."""

    model_config = ConfigDict(extra="forbid")

    buyer_part_no: str | None = None
    description: str | None = Field(default=None, min_length=1)
    quantity: Decimal | None = None
    unit_of_measure: str | None = None
    unit_price: Decimal | None = None
    delivery: str | None = None
    is_option: bool | None = None
    quoted_unit_price: Decimal | None = None
    extended_price: Decimal | None = None
    market_benchmark_price: Decimal | None = None
    price_rationale: str | None = None
    line_status: LineStatus | None = None
    clarifying_question: str | None = None
    availability_note: str | None = None


_NOT_NULL = {"description", "is_option", "line_status"}


@app.patch("/api/line-items/{line_item_id}")
def update_line_item(
    line_item_id: int,
    patch: LineItemPatch,
    release: bool = Query(default=False, description="Drop the lease after saving"),
    user: User = Depends(current_user),
    conn: Any = Depends(db),
) -> dict[str, Any]:
    """Write a row the caller holds the lease on.

    Raises:
        HTTPException: 409 if the caller does not hold a live lease on the row.
    """
    fields = patch.model_dump(exclude_unset=True)
    for name in _NOT_NULL & fields.keys():
        if fields[name] is None:
            raise HTTPException(422, f"{name} cannot be null")
    with conn.transaction():
        # FOR UPDATE on the lease row blocks a concurrent steal until we commit.
        held = conn.execute(
            """
            SELECT 1 FROM line_item_locks
            WHERE line_item_id = %s AND user_id = %s AND expires_at > now()
            FOR UPDATE
            """,
            (line_item_id, user.id),
        ).fetchone()
        if held is None:
            raise _lock_conflict(conn, line_item_id)
        if fields:
            assignments = ", ".join(f"{name} = %({name})s" for name in fields)
            conn.execute(
                f"UPDATE line_items SET {assignments} WHERE id = %(id)s",
                {**fields, "id": line_item_id},
            )
        if release:
            conn.execute("DELETE FROM line_item_locks WHERE line_item_id = %s", (line_item_id,))
        else:
            conn.execute(
                """
                UPDATE line_item_locks
                SET expires_at = now() + make_interval(secs => %s)
                WHERE line_item_id = %s
                """,
                (LOCK_TTL_SECONDS, line_item_id),
            )
    return _fetch_line(conn, line_item_id)


ComplianceStatus = Literal["required", "document_missing", "satisfied", "waived", "send_to_buyer"]


class CompliancePatch(BaseModel):
    """Reviewer-editable compliance columns. Omitted fields are left untouched."""

    model_config = ConfigDict(extra="forbid")

    status: ComplianceStatus | None = None
    buyer_message: str | None = None


@app.patch("/api/compliance/{compliance_id}")
def update_compliance(
    compliance_id: int,
    patch: CompliancePatch,
    _user: User = Depends(current_user),
    conn: Any = Depends(db),
) -> dict[str, Any]:
    """Update a compliance row's review status (quote-level; no row lease, last write wins).

    Raises:
        HTTPException: 404 if the row is gone, 422 if status is null.
    """
    fields = patch.model_dump(exclude_unset=True)
    if "status" in fields and fields["status"] is None:
        raise HTTPException(422, "status cannot be null")
    if fields:
        assignments = ", ".join(f"{name} = %({name})s" for name in fields)
        conn.execute(
            f"UPDATE compliance SET {assignments} WHERE id = %(id)s",
            {**fields, "id": compliance_id},
        )
    row = conn.execute(
        f"SELECT {_COMPLIANCE_COLUMNS} FROM compliance WHERE id = %s", (compliance_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "compliance row not found")
    return row


# --------------------------------------------------------------------------- static

if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404)
        # Top-level files from frontend/public (favicon etc.); everything else is the SPA.
        candidate = (FRONTEND_DIST / path).resolve()
        if path and candidate.parent == FRONTEND_DIST.resolve() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


def run() -> None:
    import uvicorn

    port = int(os.environ.get("REVIEW_PORT", "8014"))
    uvicorn.run("review_service.main:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    run()
