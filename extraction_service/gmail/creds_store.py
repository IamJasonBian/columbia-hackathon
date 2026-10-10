"""Where connected-mailbox refresh tokens live.

- ``file`` (default): ``app_storage/email_oauth.json`` (mode 0600), for local
  hackathon runs with no GCP access.
- ``firestore``: the same ``email_oauth/{email}`` docs postings-backend
  reads/writes (project $GCP_PROJECT, default jobley-501723), so a mailbox
  already connected through Jobley onboarding works here with no re-consent.

Pick with EMAIL_OAUTH_STORE=file|firestore.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import requests

from extraction_service.gmail.oauth import GmailCredentials

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "app_storage" / "email_oauth.json"


class CredsStore(Protocol):
    def get(self, email: str) -> GmailCredentials | None: ...
    def put(self, creds: GmailCredentials) -> None: ...
    def list_emails(self) -> list[str]: ...


class FileCredsStore:
    """JSON file keyed by mailbox address."""

    def __init__(self, path: Path = DEFAULT_FILE):
        self.path = path

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def get(self, email: str) -> GmailCredentials | None:
        row = self._load().get(email.lower())
        return GmailCredentials(**row) if row else None

    def put(self, creds: GmailCredentials) -> None:
        rows = self._load()
        rows[creds.email.lower()] = asdict(creds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=2))
        os.chmod(self.path, 0o600)

    def list_emails(self) -> list[str]:
        return sorted(self._load())


class FirestoreCredsStore:
    """Firestore REST on ``email_oauth/{email}`` (postings-backend layout)."""

    def __init__(self, project: str | None = None):
        self.project = project or os.environ.get("GCP_PROJECT", "jobley-501723")
        self.base = (
            f"https://firestore.googleapis.com/v1/projects/{self.project}"
            "/databases/(default)/documents/email_oauth"
        )

    def _token(self) -> str:
        try:
            import google.auth
            from google.auth.transport.requests import Request

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/datastore"])
            creds.refresh(Request())
            return creds.token
        except Exception:
            # Same fallback postings-backend uses off-GCP.
            return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def get(self, email: str) -> GmailCredentials | None:
        resp = requests.get(f"{self.base}/{quote(email.lower(), safe='')}", headers=self._headers(), timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        fields = {k: v.get("stringValue", "") for k, v in resp.json().get("fields", {}).items()}
        if not (fields.get("client_id") and fields.get("client_secret") and fields.get("refresh_token")):
            return None
        return GmailCredentials(
            email=email.lower(),
            client_id=fields["client_id"],
            client_secret=fields["client_secret"],
            refresh_token=fields["refresh_token"],
            token_uri=fields.get("token_uri") or GmailCredentials.token_uri,
            onboarded_at=fields.get("onboarded_at"),
        )

    def put(self, creds: GmailCredentials) -> None:
        body = {
            "fields": {
                k: {"stringValue": v}
                for k, v in {
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "refresh_token": creds.refresh_token,
                    "onboarded_at": creds.onboarded_at or "",
                }.items()
            }
        }
        resp = requests.patch(
            f"{self.base}/{quote(creds.email.lower(), safe='')}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()

    def list_emails(self) -> list[str]:
        resp = requests.get(
            self.base, headers=self._headers(), params={"mask.fieldPaths": "onboarded_at"}, timeout=15
        )
        resp.raise_for_status()
        names = [d["name"].rsplit("/", 1)[-1] for d in resp.json().get("documents", [])]
        return sorted(requests.utils.unquote(n) for n in names)


def default_store() -> CredsStore:
    """Build the store selected by EMAIL_OAUTH_STORE.

    Returns:
        A file store (default) or the Firestore store.
    """
    if os.environ.get("EMAIL_OAUTH_STORE", "file").lower() == "firestore":
        return FirestoreCredsStore()
    return FileCredsStore(Path(os.environ["EMAIL_OAUTH_FILE"]) if os.environ.get("EMAIL_OAUTH_FILE") else DEFAULT_FILE)
