"""Local SA ID-token minting (from mclane_data_pipeline token_provider.py)."""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

import requests as http_requests
import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account

_IMPERSONATION_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class TokenProviderError(RuntimeError):
    pass


class ServiceAccountIdTokenProvider:
    def __init__(
        self,
        audience: str,
        service_account_file: Optional[str] = None,
        impersonate_service_account: Optional[str] = None,
        refresh_after_seconds: int = 3500,
    ):
        if not audience:
            raise ValueError("audience must be provided")
        self.audience = audience
        self.service_account_file = service_account_file
        self.impersonate_service_account = impersonate_service_account
        self.refresh_after_seconds = int(refresh_after_seconds)
        self._token: Optional[str] = None
        self._token_obtained_at: float = 0.0
        self._lock = threading.Lock()
        self._request = Request()
        self._source_credentials = None
        if self.impersonate_service_account and not self.service_account_file:
            self._source_credentials, _ = google.auth.default(
                scopes=_IMPERSONATION_SCOPES
            )

    def _needs_refresh(self) -> bool:
        if not self._token:
            return True
        return (time.time() - self._token_obtained_at) >= self.refresh_after_seconds

    def _mint_token(self) -> str:
        try:
            if self.service_account_file:
                creds = service_account.IDTokenCredentials.from_service_account_file(
                    self.service_account_file, target_audience=self.audience
                )
                creds.refresh(self._request)
                token = getattr(creds, "token", None)
            elif self.impersonate_service_account:
                target_credentials = impersonated_credentials.Credentials(
                    source_credentials=self._source_credentials,
                    target_principal=self.impersonate_service_account,
                    target_scopes=_IMPERSONATION_SCOPES,
                    lifetime=3600,
                )
                id_creds = impersonated_credentials.IDTokenCredentials(
                    target_credentials=target_credentials,
                    target_audience=self.audience,
                    include_email=True,
                )
                id_creds.refresh(self._request)
                token = getattr(id_creds, "token", None)
            else:
                response = http_requests.get(
                    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
                    headers={"Metadata-Flavor": "Google"},
                    params={"audience": self.audience},
                    timeout=10,
                )
                response.raise_for_status()
                token = response.text.strip()
            if not token:
                raise TokenProviderError("Failed to obtain ID token")
            return token
        except TokenProviderError:
            raise
        except Exception as exc:
            raise TokenProviderError(f"Failed to mint ID token: {exc}") from exc

    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and not self._needs_refresh():
            assert self._token is not None
            return self._token
        with self._lock:
            if not force_refresh and not self._needs_refresh():
                assert self._token is not None
                return self._token
            token = self._mint_token()
            self._token = token
            self._token_obtained_at = time.time()
            return token


def backend_token_provider() -> ServiceAccountIdTokenProvider:
    audience = os.environ.get("BACKEND_TOKEN_AUDIENCE") or os.environ.get("BACKEND_URL")
    if not audience:
        raise TokenProviderError(
            "Set BACKEND_TOKEN_AUDIENCE or BACKEND_URL for the token audience"
        )
    return ServiceAccountIdTokenProvider(
        audience=audience,
        service_account_file=os.environ.get("BACKEND_SA_FILE") or None,
        impersonate_service_account=os.environ.get("BACKEND_IMPERSONATE_SERVICE_ACCOUNT")
        or None,
    )
