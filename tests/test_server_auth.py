from fastapi.testclient import TestClient

import server


def test_task_api_requires_bearer_token(monkeypatch):
    monkeypatch.delenv("SKIP_AUTH", raising=False)
    monkeypatch.setenv("LOCAL_SA_TOKEN", "test-token")

    client = TestClient(server.app)
    assert client.get("/health").status_code == 401
    assert client.get(
        "/health",
        headers={"Authorization": "Bearer test-token"},
    ).status_code == 200
