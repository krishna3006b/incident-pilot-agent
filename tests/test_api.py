from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

def test_list_incidents():
    response = client.get("/api/v1/incidents")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_trigger_alert():
    payload = {
        "service_name": "payment-service",
        "summary": "HTTP 500 spike after v1.8.3 deployment",
        "severity": "P1"
    }
    response = client.post("/api/v1/alerts", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    assert "incident_id" in response.json()
