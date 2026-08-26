from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.agents.coding_agent import CodingResult


@dataclass
class OllamaWorker:
    """Internal Ollama local-fallback coding worker.

    This worker is NOT in DEFAULT_PRIORITY — it is only discoverable via
    refresh_workers() and serves as the last-resort local fallback when all
    cloud workers are exhausted.  Ollama must be running locally with a
    model pulled (default llama3.2:latest).
    """

    base_url: str | None = None
    model: str | None = None

    def __post_init__(self):
        env = os.environ
        self.base_url = (
            self.base_url
            or str(env.get("OLLAMA_HOST", "http://127.0.0.1:11434")).strip()
            or "http://127.0.0.1:11434"
        )
        self.model = (
            self.model
            or str(env.get("OLLAMA_MODEL", "llama3.2:latest")).strip()
            or "llama3.2:latest"
        )

    def is_available(self) -> bool:
        """Check if Ollama is reachable and the model is available."""
        try:
            url = self.base_url.rstrip("/") + "/api/tags"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                return any(self.model in m for m in models)
        except Exception:
            return False

    def execute(
        self, prompt: str, workspace: str, on_output=None, timeout: int | None = None
    ) -> CodingResult:
        if not self.is_available():
            return CodingResult(
                backend="ollama",
                success=False,
                error="Ollama is unavailable (not running or model not found).",
                started=False,
            )
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 2048},
        }
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        effective_timeout = timeout or 120
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return CodingResult(
                backend="ollama",
                success=False,
                error=f"Ollama HTTP error: {exc.code}",
                started=True,
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            return CodingResult(
                backend="ollama",
                success=False,
                error=f"Ollama request failed: {exc}",
                started=True,
            )
        output = str(data.get("response", "")).strip()
        if not output:
            return CodingResult(
                backend="ollama",
                success=False,
                error="Ollama returned an empty response.",
                started=True,
            )
        if on_output is not None:
            on_output(output)
        return CodingResult(
            backend="ollama", success=True, output=output, started=True, worker_name="ollama"
        )
