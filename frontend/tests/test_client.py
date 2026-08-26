from app.api.client import BackendClient

class FakeResponse:
    def __init__(self, data):
        self.data = data
    def raise_for_status(self):
        pass
    def json(self):
        return self.data

def test_client_health(monkeypatch):
    def fake_request(method, url, timeout, **kwargs):
        assert method == "GET"
        assert url.endswith("/health")
        return FakeResponse({"status": "ok"})
    monkeypatch.setattr("requests.request", fake_request)
    assert BackendClient().health()["status"] == "ok"

def test_client_create_job(monkeypatch):
    def fake_request(method, url, timeout, **kwargs):
        assert method == "POST"
        assert url.endswith("/api/v1/jobs")
        assert kwargs["json"]["name"] == "demo"
        return FakeResponse({"job_id": "abc", "status": "verified"})
    monkeypatch.setattr("requests.request", fake_request)
    result = BackendClient().create_job({
        "name": "demo", "task": "test", "command": ["pytest", "-q"]
    })
    assert result["job_id"] == "abc"
