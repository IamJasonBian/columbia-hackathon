from fastapi.testclient import TestClient

from services.validation.main import app


def test_validation_api_requires_bearer_token(monkeypatch):
    monkeypatch.delenv("SKIP_AUTH", raising=False)
    monkeypatch.setenv("LOCAL_SA_TOKEN", "test-token")

    client = TestClient(app)
    assert client.get("/validation-jobs").status_code == 401
    assert client.get(
        "/validation-jobs",
        headers={"Authorization": "Bearer test-token"},
    ).status_code == 200
