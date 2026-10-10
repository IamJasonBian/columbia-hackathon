"""Core rules from onboarding_infrastructure app/services/validation.py."""

from __future__ import annotations

import re

from pydantic import ValidationError

from services.validation.models import (
    CustomerGlobalConfig,
    CustomerPersonalConfig,
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
            customer_errors, customer_warnings = self._validate_business_rules(global_config)
            errors.extend(customer_errors)
            warnings.extend(customer_warnings)
        except ValidationError as e:
            for error in e.errors():
                field_path = " -> ".join(str(loc) for loc in error["loc"])
                errors.append(f"{field_path}: {error['msg']}")
        except Exception as e:
            errors.append(f"Unexpected validation error: {e}")
        return ConfigValidationResponse(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def validate_new_customer(
        self, existing_config: GlobalConfig, new_customer: CustomerGlobalConfig | dict
    ) -> ConfigValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            if not isinstance(new_customer, CustomerGlobalConfig):
                new_customer = CustomerGlobalConfig(**new_customer)
            existing_customers = [c.customer.lower() for c in existing_config.data]
            if new_customer.customer.lower() in existing_customers:
                errors.append(f"Customer '{new_customer.customer}' already exists in global config")
            customer_errors, customer_warnings = self._validate_single_customer_rules(new_customer)
            errors.extend(customer_errors)
            warnings.extend(customer_warnings)
        except ValidationError as e:
            for error in e.errors():
                field_path = " -> ".join(str(loc) for loc in error["loc"])
                errors.append(f"New customer {field_path}: {error['msg']}")
        except Exception as e:
            errors.append(f"Unexpected validation error for new customer: {e}")
        return ConfigValidationResponse(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def validate_customer_personal_config(self, config_dict: dict) -> ConfigValidationResponse:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            CustomerPersonalConfig(**config_dict)
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
        names = [c.customer.lower() for c in global_config.data]
        if len(names) != len(set(names)):
            errors.append("Duplicate customer names in global config")
        for customer in global_config.data:
            e, w = self._validate_single_customer_rules(customer)
            errors.extend(e)
            warnings.extend(w)
        return errors, warnings

    def _validate_single_customer_rules(
        self, customer: CustomerGlobalConfig
    ) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not self._is_valid_email(customer.email):
            errors.append(f"Customer '{customer.customer}': invalid email '{customer.email}'")
        if not customer.bucketname.endswith("_prod"):
            warnings.append(
                f"Customer '{customer.customer}': bucket '{customer.bucketname}' does not end with '_prod'"
            )
        if not re.match(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$", customer.project_id):
            errors.append(f"Customer '{customer.customer}': invalid GCP project_id '{customer.project_id}'")
        return errors, warnings

    def _is_valid_email(self, email: str) -> bool:
        return bool(self._EMAIL_RE.match(email.strip()))


validation_service = ValidationService()
