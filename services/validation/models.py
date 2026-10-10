"""Trimmed models from onboarding_infrastructure ValidationService."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ValidationKind(str, Enum):
    global_config = "global_config"
    new_customer = "new_customer"
    customer_personal = "customer_personal"


class CustomerGlobalConfig(BaseModel):
    customer: str
    email: str
    baseId: str
    tableIdOrName: str
    bucketname: str
    project_id: str
    collectionname: str | None = None


class GlobalConfig(BaseModel):
    data: list[CustomerGlobalConfig] = Field(default_factory=list)


class CustomerPersonalConfig(BaseModel):
    customer: str
    bucket: str
    sku_mappings: dict[str, str] | None = None
    entity_names: dict[str, str] | None = None


class ConfigValidationResponse(BaseModel):
    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ValidationJobCreate(BaseModel):
    kind: ValidationKind
    payload: dict[str, Any]
    label: str | None = None


class ValidationJobPatch(BaseModel):
    label: str | None = None
    payload: dict[str, Any] | None = None
