"""Read-only Gmail REST client: search, then fetch each message as raw RFC 822.

Raw messages go straight into extraction_service.extractors.extract_document
as .eml, so every PDF/spreadsheet/image attachment is extracted by the same
code path as a manual upload.
"""

from __future__ import annotations

import base64

import requests

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Default onboarding sweep: anything in the last 30 days carrying a PDF.
DEFAULT_QUERY = "has:attachment filename:pdf newer_than:30d"


class GmailClient:
    def __init__(self, access_token: str, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"

    def search(self, query: str = DEFAULT_QUERY, max_messages: int = 25) -> list[str]:
        """Message IDs matching a Gmail search, newest first.

        Args:
            query: Gmail search syntax.
            max_messages: Upper bound on IDs returned (paginates as needed).

        Returns:
            Message IDs.
        """
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < max_messages:
            params: dict[str, str | int] = {"q": query, "maxResults": min(100, max_messages - len(ids))}
            if page_token:
                params["pageToken"] = page_token
            resp = self._session.get(f"{API}/messages", params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            ids += [m["id"] for m in body.get("messages", [])]
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_messages]

    def get_raw(self, message_id: str) -> bytes:
        """Fetch one message as raw RFC 822 bytes.

        Args:
            message_id: Gmail message ID.

        Returns:
            The decoded .eml bytes, attachments included.
        """
        resp = self._session.get(f"{API}/messages/{message_id}", params={"format": "raw"}, timeout=60)
        resp.raise_for_status()
        raw = resp.json()["raw"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
