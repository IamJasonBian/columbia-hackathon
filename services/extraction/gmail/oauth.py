"""Gmail OAuth refresh-token credentials and access-token minting."""

from __future__ import annotations

from dataclasses import dataclass

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"


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
