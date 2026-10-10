"""Trimmed models from onboarding_infrastructure ValidationService."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ValidationKind(str, Enum):
    global_config = "global_config"
    new_brand = "new_brand"
    brand_personal = "brand_personal"


class BrandGlobalConfig(BaseModel):
    brand: str
    baseId: str
    tableIdOrName: str
    bucketname: str
    project_id: str
    email: list[str] | None = None
    collectionname: str | None = None


class GlobalConfig(BaseModel):
    data: list[BrandGlobalConfig] = Field(default_factory=list)


class BrandPersonalConfig(BaseModel):
    brand: str
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
