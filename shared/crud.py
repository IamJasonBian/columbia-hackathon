"""Generic in-memory CRUD store (hackathon stand-in for GCS / postgres backend)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class CrudMeta(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CrudStore(Generic[T]):
    """Thread-unsafe JSON-document CRUD; good enough for local demos."""

    def __init__(self, item_model: type[T], prefix: str = "item"):
        self._item_model = item_model
        self._prefix = prefix
        self._rows: dict[str, dict[str, Any]] = {}

    def create(self, payload: T | dict[str, Any]) -> dict[str, Any]:
        data = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload)
        meta = CrudMeta(id=f"{self._prefix}-{uuid.uuid4().hex[:12]}")
        row = {**meta.model_dump(mode="json"), **data}
        self._rows[meta.id] = row
        return row

    def get(self, item_id: str) -> dict[str, Any] | None:
        return self._rows.get(item_id)

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        filter_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self._rows.values())
        if filter_fn:
            rows = [r for r in rows if filter_fn(r)]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[offset : offset + limit]

    def update(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        row = self._rows.get(item_id)
        if row is None:
            return None
        reserved = {"id", "created_at"}
        for key, value in patch.items():
            if key not in reserved:
                row[key] = value
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        return row

    def delete(self, item_id: str) -> bool:
        return self._rows.pop(item_id, None) is not None
