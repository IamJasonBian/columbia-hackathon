"""Core rules from onboarding_infrastructure app/services/validation.py."""

from __future__ import annotations

import re

from pydantic import ValidationError

from validation_service.models import (
    BrandGlobalConfig,
    BrandPersonalConfig,
    ConfigValidationResponse,
    GlobalConfig,
)


class ValidationService:
    _EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

    def validate_global_config(self, config_dict: dict) -> ConfigValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            global_config = GlobalConfig(**config_dict)
            brand_errors, brand_warnings = self._validate_business_rules(global_config)
            errors.extend(brand_errors)
            warnings.extend(brand_warnings)
        except ValidationError as e:
            for error in e.errors():
                field_path = " -> ".join(str(loc) for loc in error["loc"])
                errors.append(f"{field_path}: {error['msg']}")
        except Exception as e:
            errors.append(f"Unexpected validation error: {e}")
        return ConfigValidationResponse(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def validate_new_brand(
        self, existing_config: GlobalConfig, new_brand: BrandGlobalConfig | dict
    ) -> ConfigValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            if not isinstance(new_brand, BrandGlobalConfig):
                new_brand = BrandGlobalConfig(**new_brand)
            existing_brands = [b.brand.lower() for b in existing_config.data]
            if new_brand.brand.lower() in existing_brands:
                errors.append(f"Brand '{new_brand.brand}' already exists in global config")
            if new_brand.email:
                for email in new_brand.email:
                    if email and not self._is_valid_email(email):
                        errors.append(f"Invalid email format: {email}")
            brand_errors, brand_warnings = self._validate_single_brand_rules(new_brand)
            errors.extend(brand_errors)
            warnings.extend(brand_warnings)
        except ValidationError as e:
            for error in e.errors():
                field_path = " -> ".join(str(loc) for loc in error["loc"])
                errors.append(f"New brand {field_path}: {error['msg']}")
        except Exception as e:
            errors.append(f"Unexpected validation error for new brand: {e}")
        return ConfigValidationResponse(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def validate_brand_personal_config(self, config_dict: dict) -> ConfigValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            BrandPersonalConfig(**config_dict)
        except ValidationError as e:
            for error in e.errors():
                field_path = " -> ".join(str(loc) for loc in error["loc"])
                errors.append(f"{field_path}: {error['msg']}")
        except Exception as e:
            errors.append(f"Unexpected validation error: {e}")
        return ConfigValidationResponse(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _validate_business_rules(
        self, global_config: GlobalConfig
    ) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        names = [b.brand.lower() for b in global_config.data]
        if len(names) != len(set(names)):
            errors.append("Duplicate brand names in global config")
        for brand in global_config.data:
            e, w = self._validate_single_brand_rules(brand)
            errors.extend(e)
            warnings.extend(w)
        return errors, warnings

    def _validate_single_brand_rules(
        self, brand: BrandGlobalConfig
    ) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not brand.bucketname.endswith("_prod"):
            warnings.append(
                f"Brand '{brand.brand}': bucket '{brand.bucketname}' does not end with '_prod'"
            )
        if not re.match(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$", brand.project_id):
            errors.append(f"Brand '{brand.brand}': invalid GCP project_id '{brand.project_id}'")
        return errors, warnings

    def _is_valid_email(self, email: str) -> bool:
        return bool(self._EMAIL_RE.match(email.strip()))


validation_service = ValidationService()
