"""Gmail OAuth (read-only) — the Python twin of postings-backend's
routes/gmailOauth.ts + mfa.ts token helpers.

Flow: /gmail-oauth/start → Google consent → /gmail-oauth/callback exchanges
the code for a refresh token, reads the granting mailbox from the Gmail
profile endpoint, and stores creds keyed by THAT address (never a query
param), so a visitor can only ever connect their own inbox.

Client: GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET, falling back to
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET — same env names as postings-backend,
so the jobley OAuth web client can be reused (register this service's
callback URL on it).
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# Pending consent nonces (CSRF guard on the callback) live this long.
STATE_TTL_SECONDS = 15 * 60


class GmailOAuthError(RuntimeError):
    """OAuth is misconfigured or Google rejected a request."""


@dataclass
class GmailCredentials:
    email: str
    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str = TOKEN_URL
    onboarded_at: str | None = None


def client_id() -> str | None:
    return os.environ.get("GMAIL_OAUTH_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID")


def client_secret() -> str | None:
    return os.environ.get("GMAIL_OAUTH_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")


def is_configured() -> bool:
    return bool(client_id() and client_secret())


class StateStore:
    """In-memory one-shot nonces binding a callback to a /start we issued."""

    def __init__(self) -> None:
        self._pending: dict[str, float] = {}

    def issue(self) -> str:
        now = time.time()
        self._pending = {k: exp for k, exp in self._pending.items() if exp > now}
        nonce = secrets.token_urlsafe(24)
        self._pending[nonce] = now + STATE_TTL_SECONDS
        return nonce

    def consume(self, nonce: str) -> bool:
        expires = self._pending.pop(nonce, None)
        return expires is not None and expires > time.time()


def consent_url(redirect_uri: str, state: str, login_hint: str | None = None) -> str:
    """Build the Google consent URL for offline read-only Gmail access.

    Args:
        redirect_uri: Callback URL registered on the OAuth client.
        state: Opaque value echoed back to the callback.
        login_hint: Mailbox to preselect on the Google account chooser.

    Returns:
        The consent URL to redirect the browser to.

    Raises:
        GmailOAuthError: Client ID/secret are not set.
    """
    if not is_configured():
        raise GmailOAuthError(
            "Gmail OAuth not configured (set GMAIL_OAUTH_CLIENT_ID/SECRET or GOOGLE_CLIENT_ID/SECRET)"
        )
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Force consent so Google always returns a refresh token.
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> GmailCredentials:
    """Trade an authorization code for creds keyed by the granting mailbox.

    Args:
        code: The ``code`` query param Google sent to the callback.
        redirect_uri: Must equal the one used on the consent URL.

    Returns:
        Stored-ready credentials, ``email`` taken from the Gmail profile.

    Raises:
        GmailOAuthError: Token exchange or profile lookup failed, or Google
            issued no refresh token (a prior grant must be revoked first).
    """
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not resp.ok:
        raise GmailOAuthError(f"token exchange HTTP {resp.status_code}: {resp.text[:300]}")
    tokens = resp.json()
    if not tokens.get("refresh_token") or not tokens.get("access_token"):
        raise GmailOAuthError(
            "Google returned no refresh token — revoke this app at "
            "myaccount.google.com/permissions and connect again"
        )
    email = mailbox_address(tokens["access_token"])
    return GmailCredentials(
        email=email,
        client_id=client_id() or "",
        client_secret=client_secret() or "",
        refresh_token=tokens["refresh_token"],
        onboarded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def access_token(creds: GmailCredentials) -> str:
    """Mint a short-lived access token from stored refresh creds.

    Args:
        creds: Stored credentials.

    Returns:
        A bearer access token.

    Raises:
        GmailOAuthError: The refresh failed (revoked grant, bad client).
    """
    resp = requests.post(
        creds.token_uri or TOKEN_URL,
        data={
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if not resp.ok:
        raise GmailOAuthError(f"token refresh HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["access_token"]


def mailbox_address(token: str) -> str:
    """The lowercase address of the mailbox a token belongs to.

    Args:
        token: A Gmail access token.

    Returns:
        The mailbox email address.

    Raises:
        GmailOAuthError: The profile call failed.
    """
    resp = requests.get(PROFILE_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if not resp.ok:
        raise GmailOAuthError(f"Gmail profile HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["emailAddress"].lower()
