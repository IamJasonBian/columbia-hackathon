from __future__ import annotations

from services.validation.logic import validation_service
from services.validation.models import GlobalConfig


def _customer(name: str = "acme", email: str = "buyer@acme.com") -> dict:
    return {
        "customer": name,
        "email": email,
        "baseId": "app123",
        "tableIdOrName": "Orders",
        "bucketname": f"{name}_prod",
        "project_id": "acme-project",
    }


def test_global_config_requires_valid_unique_customers_with_email():
    no_email = _customer(name="beta")
    del no_email["email"]
    assert any("email" in e for e in validation_service.validate_global_config({"data": [no_email]}).errors)

    result = validation_service.validate_global_config(
        {"data": [_customer(email="not-an-email"), _customer()]}
    )
    assert "Duplicate customer names in global config" in result.errors
    assert "Customer 'acme': invalid email 'not-an-email'" in result.errors


def test_new_customer_rejects_existing_name_and_accepts_new_one():
    existing = GlobalConfig(data=[_customer()])

    assert validation_service.validate_new_customer(existing, _customer(name="ACME")).errors == [
        "Customer 'ACME' already exists in global config"
    ]
    assert validation_service.validate_new_customer(existing, _customer(name="globex")).is_valid
