from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

def test_status():
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    assert "diagnose" in r.json()["workflow"]

def test_demo_job_and_event_history(tmp_path):
    r = client.post("/api/v1/jobs", json={
        "name": "demo",
        "task": "create demo add function and test it",
        "workspace": str(tmp_path),
        "max_attempts": 3,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "verified"
    assert data["attempts"] == 1

    detail = client.get(f"/api/v1/jobs/{data['job_id']}")
    assert detail.status_code == 200
    body = detail.json()
    names = [e["event"] for e in body["events"]]
    assert "generate" in names
    assert "diagnose" in names
    assert "fix" in names
    assert "verify" in names
    assert "checkpoint" in names
