from fastapi.testclient import TestClient
from app.main import app
from app.security import validate_command
import pytest

client = TestClient(app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["version"] == "1.0.0"

def test_status():
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    assert r.json()["provider"] == "deterministic"

def test_verified_autofix_pipeline(tmp_path):
    r = client.post("/api/v1/jobs", json={
        "name": "demo",
        "task": "create demo add function and test it",
        "workspace": str(tmp_path),
        "max_attempts": 3
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "verified"
    assert data["attempts"] == 1

    detail = client.get(f"/api/v1/jobs/{data['job_id']}")
    assert detail.status_code == 200
    names = [e["event"] for e in detail.json()["events"]]
    for required in ["plan", "scan", "generate", "test", "diagnose", "fix", "verify", "checkpoint"]:
        assert required in names

def test_unfixable_project_fails_and_rolls_back(tmp_path):
    (tmp_path / "test_bad.py").write_text(
        "def test_bad():\n    assert 1 == 2\n", encoding="utf-8"
    )
    r = client.post("/api/v1/jobs", json={
        "name": "bad",
        "task": "nothing",
        "workspace": str(tmp_path),
        "max_attempts": 1
    })
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert not (tmp_path / ".autofix_checkpoint.json").exists()

def test_security():
    validate_command(["pytest", "-q"])
    validate_command(["python", "-m", "pytest", "-q"])
    with pytest.raises(ValueError):
        validate_command(["powershell", "Get-ChildItem"])
    with pytest.raises(ValueError):
        validate_command(["python", "-c", "x", "&&", "whoami"])

def test_missing_job():
    assert client.get("/api/v1/jobs/not-found").status_code == 404
