"""Generic CRUD + run endpoint for onboarding ValidationService."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from shared.auth import require_bearer
from shared.crud import CrudStore
from validation_service.logic import validation_service
from validation_service.models import (
    ConfigValidationResponse,
    GlobalConfig,
    ValidationJobCreate,
    ValidationJobPatch,
    ValidationKind,
)

app = FastAPI(title="Validation CRUD", version="0.1.0")
store = CrudStore[dict](dict, prefix="vjob")  # type: ignore[type-var]


class RunBody(BaseModel):
    existing_global: dict[str, Any] | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "validation"}


@app.post("/validation-jobs", dependencies=[Depends(require_bearer)])
def create_job(body: ValidationJobCreate) -> dict[str, Any]:
    row = store.create(
        {
            "kind": body.kind.value,
            "label": body.label,
            "payload": body.payload,
            "last_result": None,
        }
    )
    return row


@app.get("/validation-jobs", dependencies=[Depends(require_bearer)])
def list_jobs(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return store.list(limit=limit, offset=offset)


@app.get("/validation-jobs/{job_id}", dependencies=[Depends(require_bearer)])
def get_job(job_id: str) -> dict[str, Any]:
    row = store.get(job_id)
    if not row:
        raise HTTPException(404, "validation job not found")
    return row


@app.patch("/validation-jobs/{job_id}", dependencies=[Depends(require_bearer)])
def patch_job(job_id: str, body: ValidationJobPatch) -> dict[str, Any]:
    patch = body.model_dump(exclude_unset=True)
    row = store.update(job_id, patch)
    if not row:
        raise HTTPException(404, "validation job not found")
    return row


@app.delete("/validation-jobs/{job_id}", dependencies=[Depends(require_bearer)])
def delete_job(job_id: str) -> dict[str, bool]:
    if not store.delete(job_id):
        raise HTTPException(404, "validation job not found")
    return {"deleted": True}


@app.post(
    "/validation-jobs/{job_id}/run",
    response_model=ConfigValidationResponse,
    dependencies=[Depends(require_bearer)],
)
def run_job(job_id: str, body: RunBody | None = None) -> ConfigValidationResponse:
    row = store.get(job_id)
    if not row:
        raise HTTPException(404, "validation job not found")
    kind = ValidationKind(row["kind"])
    payload = row["payload"]
    if kind == ValidationKind.global_config:
        result = validation_service.validate_global_config(payload)
    elif kind == ValidationKind.brand_personal:
        result = validation_service.validate_brand_personal_config(payload)
    elif kind == ValidationKind.new_brand:
        existing = GlobalConfig(**(body.existing_global or {"data": []}))
        result = validation_service.validate_new_brand(existing, payload)
    else:
        raise HTTPException(400, f"unknown kind {kind}")
    store.update(job_id, {"last_result": result.model_dump()})
    return result


@app.post("/validate/global", response_model=ConfigValidationResponse)
def validate_global_direct(
    config: dict[str, Any], _: None = Depends(require_bearer)
) -> ConfigValidationResponse:
    return validation_service.validate_global_config(config)


def run() -> None:
    import uvicorn

    port = int(os.environ.get("VALIDATION_PORT", "8010"))
    uvicorn.run("validation_service.main:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    run()
