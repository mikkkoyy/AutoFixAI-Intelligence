from dataclasses import dataclass
from typing import Any
import requests

@dataclass
class ApiError(Exception):
    message: str

class BackendClient:
    def __init__(self, base_url="http://127.0.0.1:8000", timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(
                method, self.base_url + path,
                timeout=self.timeout, **kwargs
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise ApiError(str(exc)) from exc

    def health(self):
        return self._request("GET", "/health")

    def status(self):
        return self._request("GET", "/api/v1/status")

    def create_job(self, payload: dict[str, Any]):
        return self._request("POST", "/api/v1/jobs", json=payload)

    def job(self, job_id):
        return self._request("GET", f"/api/v1/jobs/{job_id}")

    def events(self, job_id):
        return self._request("GET", f"/api/v1/jobs/{job_id}/events")
