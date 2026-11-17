from fastapi.testclient import TestClient

from backend.main import app


client = TestClient(app)


def test_demo_overview():
    response = client.get("/v1/demo/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace"] == "Acme Commerce"
    assert payload["featured_signal"]["status"] == "DECREASE"


def test_demo_collect():
    response = client.post("/v1/demo/collect")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "COMPLETED"
    assert payload["observation"]["confidence"] == "HIGH"
    assert payload["alert"]["triggered"] is True
