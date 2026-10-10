-- Review UI: users, sessions, and per-row edit leases on doc_lines.lines.
-- Applied on services.review startup; needs the pipeline schema (bootstrap/init_postgres.py) first.

CREATE TABLE IF NOT EXISTS app_users (
    id BIGSERIAL PRIMARY KEY,
    google_sub TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    name TEXT,
    picture TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user ON app_sessions (user_id);

-- One row = one lease. A lease past expires_at is free for anyone to take.
-- Cascades away when the pipeline replaces a quote's lines.
-- Leases are short-lived, so the legacy public.line_item_locks table is dropped, not migrated.
DROP TABLE IF EXISTS line_item_locks;
CREATE TABLE IF NOT EXISTS doc_lines.line_locks (
    line_id BIGINT PRIMARY KEY REFERENCES doc_lines.lines(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_line_locks_user ON doc_lines.line_locks (user_id);
