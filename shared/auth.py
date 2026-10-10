"""Optional Bearer check using a locally minted ID token."""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def require_bearer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if os.environ.get("SKIP_AUTH", "").lower() in ("1", "true", "yes"):
        return
    expected = os.environ.get("LOCAL_SA_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Set LOCAL_SA_TOKEN (run bootstrap/mint_sa_token.py) or SKIP_AUTH=1",
        )
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
